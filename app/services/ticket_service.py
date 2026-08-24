import uuid
from datetime import datetime

from app.core.authorization import Principal, authorize_ticket_access
from app.core.clock import now
from app.core.pagination import encode_cursor
from app.core.unit_of_work import SqlAlchemyUnitOfWork
from app.domain import sla
from app.domain.errors import DomainError
from app.domain.state_machine import is_legal_transition, is_role_authorized
from app.models.enums import ActorType, EventType, Priority, Role, TicketStatus
from app.models.ticket import Ticket
from app.models.ticket_event import TicketEvent
from app.schemas.ticket import TicketCreate


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

        # 3. Calculate SLA deadline
        created_at = now()
        deadline = sla.deadline_for(created_at, priority_enum)

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
        ticket = await self.get_ticket(principal, ticket_id)
        if not ticket:
            from app.domain.errors import NotFound

            raise NotFound("Ticket not found")

        from_status = ticket.status

        # Legality first: an illegal (from -> to) pair is 409 regardless of who asks
        # (API.md §5). Checking it before the role gate avoids telling an
        # unauthorized caller which transitions would have been legal.
        if not is_legal_transition(from_status, to_status):
            from app.domain.errors import StateConflict

            raise StateConflict("Illegal status transition")

        # Role gate for this specific transition (INV-4, TICKET_LIFECYCLE.md §2).
        # `get_ticket` above only proved the principal may *see* the ticket; seeing
        # it is not permission to drive it. Without this, a customer could resolve
        # their own ticket and a dispatcher could start work.
        if not is_role_authorized(principal.role, from_status, to_status):
            from app.domain.errors import Forbidden

            raise Forbidden("Your role cannot perform this transition")

        # Object-level narrowing: "AGENT" in the table means the *assigned* agent.
        if principal.role == Role.AGENT and ticket.assignee_id != principal.id:
            from app.domain.errors import NotFound

            raise NotFound("Ticket not found")

        # Guard: Some transitions might need additional logic.
        # e.g., T1 guard on unassigned ticket (handled in P4 perhaps, but we can do a simple check)
        # Actually, TICKET_LIFECYCLE.md says OPEN -> IN_PROGRESS requires an assignee.
        if to_status == TicketStatus.IN_PROGRESS and not ticket.assignee_id:
            from app.domain.errors import BusinessRuleViolation

            raise BusinessRuleViolation("Cannot start progress on unassigned ticket")

        # Determine if we should set resolved_at
        resolved_at = None
        if to_status == TicketStatus.RESOLVED:
            resolved_at = now()

        # Guarded conditional update
        success = await self.uow.tickets.transition_if(
            ticket_id=ticket.id,
            expected_from=from_status,
            to_status=to_status,
            resolved_at=resolved_at,
        )

        if not success:
            from app.domain.errors import StateConflict

            raise StateConflict("State conflict or concurrent modification")

        # Create STATUS_CHANGE event
        actor_type = ActorType.CUSTOMER if principal.type == ActorType.CUSTOMER else ActorType.USER
        event = TicketEvent(
            id=uuid.uuid4(),
            ticket_id=ticket.id,
            type=EventType.STATUS_CHANGE,
            actor_type=actor_type,
            actor_id=principal.id,
            from_status=from_status,
            to_status=to_status,
            created_at=now(),
        )
        self.uow.events.insert(event)

        # Fetch updated ticket
        updated_ticket = await self.uow.tickets.get(ticket.id)
        assert updated_ticket is not None
        return updated_ticket

    async def list_tickets(
        self,
        principal: Principal,
        *,
        status: TicketStatus | None = None,
        priority: Priority | None = None,
        breached: bool | None = None,
        assigned: bool | None = None,
        limit: int = 50,
        cursor: tuple[datetime, uuid.UUID] | None = None,
    ) -> tuple[list[Ticket], str | None]:
        """Tickets this principal may list, plus the cursor for the next page.

        Read scope per AUTHORIZATION.md §3: customers see their own, agents see
        what is assigned to them, dispatchers and admins see everything (INV-9).
        """
        scope: dict[str, uuid.UUID] = {}
        if principal.type == ActorType.CUSTOMER:
            scope["customer_id"] = principal.id
        elif principal.role == Role.AGENT:
            scope["assignee_id"] = principal.id

        # One extra row tells us whether a further page exists without a COUNT.
        rows = list(
            await self.uow.tickets.list_scoped(
                **scope,
                status=status,
                priority=priority,
                breached=breached,
                assigned=assigned,
                limit=limit + 1,
                cursor=cursor,
            )
        )

        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor = encode_cursor(page[-1].created_at, page[-1].id) if has_more and page else None
        return page, next_cursor
