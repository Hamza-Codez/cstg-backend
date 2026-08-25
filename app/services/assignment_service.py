import uuid

from app.core.authorization import Principal
from app.core.clock import now
from app.core.unit_of_work import SqlAlchemyUnitOfWork
from app.domain.assignment import Candidate, Strategy, select
from app.domain.errors import BusinessRuleViolation, Forbidden, NotFound, StateConflict
from app.models.enums import ActorType, EventType, Role
from app.models.ticket import Ticket
from app.models.ticket_event import TicketEvent
from app.models.user import AppUser


class AssignmentService:
    def __init__(self, uow: SqlAlchemyUnitOfWork):
        self.uow = uow

    async def _candidates(self, *, for_automation: bool) -> list[Candidate]:
        """Build the choice set. INV-16 lives in the repository predicate."""
        agents = await self.uow.assignment.eligible_agents(for_automation=for_automation)
        loads = await self.uow.assignment.open_counts()
        last = await self.uow.assignment.last_assigned_at()
        return [
            Candidate(
                user_id=a.id,
                open_tickets=loads.get(a.id, 0),
                max_open_tickets=a.max_open_tickets,
                last_assigned_at=last.get(a.id),
            )
            for a in agents
        ]

    async def _assert_capacity(self, agent: AppUser, *, self_claim: bool) -> None:
        """Capacity is a routing heuristic, not an invariant.

        Refused as 422 rather than silently, because an agent deliberately
        taking work — or a dispatcher deliberately routing it — deserves to know
        why it did not happen.
        """
        if agent.max_open_tickets is None:
            return
        open_now = await self.uow.assignment.open_count_for(agent.id)
        if open_now >= agent.max_open_tickets:
            if self_claim:
                raise BusinessRuleViolation(
                    f"You're at your ticket limit ({agent.max_open_tickets}). "
                    "Resolve something first."
                )
            raise BusinessRuleViolation(
                f"{agent.name} is at their ticket limit ({agent.max_open_tickets})."
            )

    async def claim_ticket(self, principal: Principal, ticket_id: uuid.UUID) -> Ticket:
        """An agent takes an unassigned ticket themselves (spec07 §4).

        Claiming is *taking*, not reassigning: the guard requires the ticket to
        be unassigned, so an agent cannot take a colleague's work. That is a
        dispatcher action, and the 409 says so.
        """
        if principal.role not in (Role.AGENT, Role.ADMIN):
            raise Forbidden("Only agents can claim tickets")

        ticket = await self.uow.tickets.get(ticket_id)
        if not ticket:
            raise NotFound("Ticket not found")

        agent = await self.uow.users.get_active_agent(principal.id)
        if not agent:
            raise BusinessRuleViolation("Only an active agent can claim a ticket")
        await self._assert_capacity(agent, self_claim=True)

        claimed = await self.uow.tickets.claim_if_unassigned(
            ticket_id=ticket_id, assignee_id=principal.id
        )
        if not claimed:
            # A lost race is the NORMAL outcome when two agents scan the same
            # queue, not a fault.
            raise StateConflict("Someone else just took this one.")

        self._record_assignment(ticket_id, principal.id, actor_id=principal.id)
        updated = await self.uow.tickets.get(ticket_id)
        assert updated is not None
        return updated

    async def auto_assign(self, ticket_id: uuid.UUID) -> uuid.UUID | None:
        """Route a new ticket automatically, or leave it unassigned.

        Returns the chosen agent, or None. `None` is a normal outcome and must
        not fail the caller: a ticket nobody can take is a dispatcher's problem,
        which is a visible, recoverable state.
        """
        config = await self.uow.assignment.config()
        if config is None:
            return None

        strategy = Strategy(config.strategy)
        chosen = select(await self._candidates(for_automation=True), strategy)
        if chosen is None:
            return None

        assigned = await self.uow.tickets.assign_if_open_or_in_progress(
            ticket_id=ticket_id, assignee_id=chosen
        )
        if not assigned:
            return None

        # SYSTEM actor, like the SLA monitor. `system_actor_has_no_id` already
        # covers this shape: actor_id must be NULL when actor_type is SYSTEM.
        self._record_assignment(ticket_id, chosen, actor_id=None)
        return chosen

    def _record_assignment(
        self, ticket_id: uuid.UUID, assignee_id: uuid.UUID, *, actor_id: uuid.UUID | None
    ) -> None:
        self.uow.events.insert(
            TicketEvent(
                id=uuid.uuid4(),
                ticket_id=ticket_id,
                type=EventType.ASSIGNMENT,
                actor_type=ActorType.SYSTEM if actor_id is None else ActorType.USER,
                actor_id=actor_id,
                detail={"assignee_id": str(assignee_id), "automatic": actor_id is None},
                created_at=now(),
            )
        )

    async def assign_ticket(
        self,
        principal: Principal,
        ticket_id: uuid.UUID,
        assignee_id: uuid.UUID,
        *,
        override_capacity: bool = False,
    ) -> Ticket:
        # 1. Authorization: Only DISPATCHER or ADMIN can assign
        if principal.role not in (Role.DISPATCHER, Role.ADMIN):
            raise Forbidden("Only dispatchers and admins can assign tickets")

        # 2. Lookup ticket
        ticket = await self.uow.tickets.get(ticket_id)
        if not ticket:
            raise NotFound("Ticket not found")

        # 3. Validate target assignee is an active AGENT
        agent = await self.uow.users.get_active_agent(assignee_id)
        if not agent:
            raise BusinessRuleViolation("Assignee must be an active agent")

        # A dispatcher handling a CRITICAL outage must be able to say "anyway".
        # The override is deliberate, per-assignment, and recorded in the event.
        if not override_capacity:
            await self._assert_capacity(agent, self_claim=False)

        # 4. Guarded update: allowed only while OPEN or IN_PROGRESS
        success = await self.uow.tickets.assign_if_open_or_in_progress(
            ticket_id=ticket_id, assignee_id=assignee_id
        )
        if not success:
            raise StateConflict("Ticket is in a terminal state or was modified concurrently")

        # 5. Emit ASSIGNMENT event.
        #    `detail.assignee_id` is what round-robin reads to know when each
        #    agent last received work (spec07 §3) — the audit log is the single
        #    source of truth, rather than a counter column that could drift.
        event = TicketEvent(
            id=uuid.uuid4(),
            ticket_id=ticket_id,
            type=EventType.ASSIGNMENT,
            actor_type=ActorType.USER,
            actor_id=principal.id,
            detail={
                "assignee_id": str(assignee_id),
                "automatic": False,
                **({"override_capacity": True} if override_capacity else {}),
            },
            created_at=now(),
        )
        self.uow.events.insert(event)

        # Fetch and return the updated ticket
        updated_ticket = await self.uow.tickets.get(ticket_id)
        assert updated_ticket is not None
        return updated_ticket
