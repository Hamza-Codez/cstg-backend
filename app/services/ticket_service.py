import uuid
from datetime import datetime, timedelta

from app.config import get_settings
from app.core.authorization import Principal, authorize_ticket_access
from app.core.clock import now
from app.core.pagination import Cursor, InvalidCursor, encode_cursor, require_shape
from app.core.unit_of_work import SqlAlchemyUnitOfWork
from app.domain import sla
from app.domain.errors import (
    BusinessRuleViolation,
    DomainError,
    Forbidden,
    NotFound,
    StateConflict,
    ValidationError,
)
from app.domain.state_machine import (
    is_legal_transition,
    is_reopen,
    is_role_authorized,
    pauses_clock,
    resumes_clock,
)
from app.models.enums import ActorType, EventType, Role, TicketStatus
from app.models.ticket import Ticket
from app.models.ticket_event import TicketEvent
from app.repositories.ticket_repo import TransitionWrites
from app.schemas.ticket import TicketCreate
from app.schemas.ticket_filters import TicketFilters, authorize_filters


class TicketService:
    def __init__(self, uow: SqlAlchemyUnitOfWork):
        self.uow = uow

    async def create_ticket(self, principal: Principal, data: TicketCreate) -> Ticket:
        # 1. Look up customer to get their tier
        customer = await self.uow.customers.get(principal.id)
        if not customer:
            raise DomainError("Customer not found")

        # 2. Resolve priority
        priority_enum = await self.uow.priority_rules.get_priority(customer.tier, data.category)
        if not priority_enum:
            raise DomainError("Priority rule not found for customer tier and category")

        # 3. Calculate the SLA deadline under the policy active RIGHT NOW, and
        #    pin that version to the ticket. Read in the same transaction as the
        #    insert, so a policy activated mid-request cannot half-apply.
        policy_version = await self.uow.sla_policies.active_version()
        if policy_version is None:
            raise DomainError("No active SLA policy is configured")
        durations = await self.uow.sla_policies.entries_for(policy_version.id)

        created_at = now()
        deadline = sla.deadline_for(created_at, priority_enum, durations)

        # 4. Create Ticket model
        ticket = Ticket(
            id=uuid.uuid4(),
            customer_id=principal.id,
            subject=data.subject,
            body=data.body,
            category=data.category,
            priority=priority_enum,
            status=TicketStatus.OPEN,
            deadline=deadline,
            # Nothing has paused yet, so the effective due time is the
            # original promise (INV-13 holds trivially at creation).
            sla_due_at=deadline,
            # INV-15: written once, never changed. It is what lets the UI
            # explain why this ticket got the window it did.
            sla_policy_version_id=policy_version.id,
            created_at=created_at,
        )

        # 5. Create TicketEvent
        event = TicketEvent(
            id=uuid.uuid4(),
            ticket_id=ticket.id,
            type=EventType.CREATED,
            actor_type=ActorType.CUSTOMER,
            actor_id=principal.id,
            to_status=TicketStatus.OPEN,
            created_at=created_at,
        )

        # 6. Insert both atomically
        self.uow.tickets.insert(ticket)
        self.uow.events.insert(event)

        return ticket

    async def get_ticket(self, principal: Principal, ticket_id: uuid.UUID) -> Ticket | None:
        ticket = await self.uow.tickets.get(ticket_id)
        if not ticket:
            return None

        # Object-level authorization check. Raises if forbidden (hiding resource).
        authorize_ticket_access(principal, ticket)
        return ticket

    # Events a customer may see on their own ticket (docs/UIUX_FRONTEND.md §5:
    # "Customers see only public updates and replies").
    #
    # ASSIGNMENT is withheld: who works a ticket is internal routing, and the
    # event's payload carries staff ids. COMMENT is withheld here because the
    # timeline is not where replies are read — GET /comments already returns the
    # PUBLIC_REPLY-only view, so surfacing COMMENT events would risk leaking the
    # existence of internal notes through their timestamps (INV-9).
    _CUSTOMER_VISIBLE_EVENTS = frozenset(
        {EventType.CREATED, EventType.STATUS_CHANGE, EventType.SLA_BREACH}
    )

    async def get_ticket_detail(
        self, principal: Principal, ticket_id: uuid.UUID
    ) -> tuple[Ticket, list[TicketEvent]]:
        """A ticket plus the timeline this principal is allowed to see.

        Object-level authorization runs first, so a hidden ticket 404s before any
        event is read.
        """
        ticket = await self.get_ticket(principal, ticket_id)
        if not ticket:
            from app.domain.errors import NotFound

            raise NotFound("Ticket not found")

        events = list(await self.uow.events.list_by_ticket(ticket_id))
        if principal.role == Role.CUSTOMER:
            events = [e for e in events if e.type in self._CUSTOMER_VISIBLE_EVENTS]
        return ticket, events

    async def transition_ticket(
        self, principal: Principal, ticket_id: uuid.UUID, to_status: TicketStatus
    ) -> Ticket:
        """Drive a ticket through the transition table (spec05 §4, §7).

        Order is deliberate and unchanged from v1: legality, then role, then
        object-level narrowing, then guards. Checking legality before the role
        gate avoids telling an unauthorized caller which transitions would have
        been legal.
        """
        ticket = await self.get_ticket(principal, ticket_id)
        if not ticket:
            raise NotFound("Ticket not found")

        from_status = ticket.status

        # Legality first: an illegal (from -> to) pair is 409 regardless of who
        # asks (API.md §5).
        if not is_legal_transition(from_status, to_status):
            raise StateConflict("Illegal status transition")

        # Role gate for this specific transition (INV-4). `get_ticket` above only
        # proved the principal may *see* the ticket; seeing it is not permission
        # to drive it.
        if not is_role_authorized(principal.role, from_status, to_status):
            raise Forbidden("Your role cannot perform this transition")

        # Object-level narrowing. "AGENT" in the table means the *assigned*
        # agent; "CUSTOMER" means the *owning* customer — T5 and T6 are the
        # first transitions a customer can reach, so their check is explicit.
        if principal.role == Role.AGENT and ticket.assignee_id != principal.id:
            raise NotFound("Ticket not found")
        if principal.type is ActorType.CUSTOMER and ticket.customer_id != principal.id:
            raise NotFound("Ticket not found")

        # T1 guard: TICKET_LIFECYCLE.md §2 requires an assignee before work
        # starts. Reopen (T6) lands on IN_PROGRESS too but keeps its assignee,
        # so it is exempt.
        if (
            to_status == TicketStatus.IN_PROGRESS
            and from_status == TicketStatus.OPEN
            and not ticket.assignee_id
        ):
            raise BusinessRuleViolation("Cannot start progress on unassigned ticket")

        moment = now()
        if is_reopen(from_status, to_status):
            self._assert_reopen_window(ticket, moment)

        writes = self._writes_for(ticket, from_status, to_status, moment)

        success = await self.uow.tickets.transition_if(
            ticket_id=ticket.id,
            expected_from=from_status,
            to_status=to_status,
            writes=writes,
        )
        if not success:
            raise StateConflict("State conflict or concurrent modification")

        actor_type = ActorType.CUSTOMER if principal.type == ActorType.CUSTOMER else ActorType.USER
        self.uow.events.insert(
            TicketEvent(
                id=uuid.uuid4(),
                ticket_id=ticket.id,
                type=EventType.STATUS_CHANGE,
                actor_type=actor_type,
                actor_id=principal.id,
                from_status=from_status,
                to_status=to_status,
                created_at=moment,
            )
        )

        updated_ticket = await self.uow.tickets.get(ticket.id)
        assert updated_ticket is not None
        return updated_ticket

    def _writes_for(
        self,
        ticket: Ticket,
        from_status: TicketStatus,
        to_status: TicketStatus,
        moment: datetime,
    ) -> TransitionWrites:
        """The columns this transition sets besides `status`.

        Kept together so every side effect of a move is visible in one place and
        lands in the single guarded UPDATE.
        """
        if to_status == TicketStatus.RESOLVED:
            return TransitionWrites(resolved_at=moment)

        # T4 — the clock stops. sla_due_at is deliberately left stale: nothing
        # reads it while paused, because the monitor's index predicate excludes
        # PENDING_CUSTOMER (spec05 §5).
        if pauses_clock(to_status):
            return TransitionWrites(sla_paused_at=moment)

        # T5 — the clock restarts. Accrue the pause and recompute the effective
        # due time, both in the same statement as the status change.
        if resumes_clock(from_status):
            paused_at = ticket.sla_paused_at or moment
            accrued = sla.accrue_pause(paused_at, moment, ticket.sla_paused_seconds)
            return TransitionWrites(
                clear_sla_paused_at=True,
                sla_paused_seconds=accrued,
                sla_due_at=sla.due_at(ticket.deadline, accrued),
            )

        # T6 — reopen. resolved_at must be cleared (resolved_requires_status),
        # and sla_breached_at is deliberately *kept*: INV-6 says a ticket
        # breaches at most once, and a service must not be able to erase its own
        # failures by reopening. reopen_count carries the signal instead.
        if is_reopen(from_status, to_status):
            return TransitionWrites(clear_resolved_at=True, increment_reopen=True)

        return TransitionWrites()

    def _assert_reopen_window(self, ticket: Ticket, moment: datetime) -> None:
        """T6 is bounded (spec05 §4).

        Unbounded reopen would let a year-old ticket return and immediately
        breach against a year-old deadline.
        """
        settings = get_settings()
        if ticket.resolved_at is None:
            return
        window = timedelta(days=settings.reopen_window_days)
        if moment - ticket.resolved_at > window:
            raise BusinessRuleViolation(
                f"This ticket was resolved more than {settings.reopen_window_days} days ago "
                "and cannot be reopened. Open a new one."
            )

    async def list_tickets(
        self,
        principal: Principal,
        *,
        filters: TicketFilters | None = None,
        limit: int = 50,
        cursor: Cursor | None = None,
    ) -> tuple[list[Ticket], str | None]:
        """Tickets this principal may list, plus the cursor for the next page.

        Read scope per AUTHORIZATION.md §3: customers see their own, agents see
        what is assigned to them, dispatchers and admins see everything (INV-9).

        The scope is applied by the repository in the same statement as the
        search predicate, so relevance ranking can never run over rows this
        principal may not see (spec04 §3).
        """
        filters = filters or TicketFilters()

        # Capability gate before anything is read: a filter this principal may
        # not use is a 403, never a silently narrowed result set.
        authorize_filters(principal, filters)

        if cursor is not None:
            # InvalidCursor is a plain ValueError so that core/ stays free of
            # domain imports; translating it here is what puts it in the error
            # taxonomy (400) instead of escaping as a 500.
            try:
                require_shape(cursor, searching=filters.is_search)
            except InvalidCursor as exc:
                raise ValidationError(str(exc)) from exc

        scope: dict[str, uuid.UUID] = {}
        if principal.type == ActorType.CUSTOMER:
            scope["customer_id"] = principal.id
        elif principal.role == Role.AGENT:
            scope["assignee_id"] = principal.id

        # One extra row tells us whether a further page exists without a COUNT.
        rows = list(
            await self.uow.tickets.list_scoped(
                **scope,
                filters=filters,
                limit=limit + 1,
                cursor=cursor,
            )
        )

        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor = None
        if has_more and page:
            last_ticket, last_rank = page[-1]
            next_cursor = encode_cursor(last_ticket.created_at, last_ticket.id, rank=last_rank)
        return [ticket for ticket, _ in page], next_cursor
