import uuid

from app.core.authorization import Principal
from app.core.clock import now
from app.core.unit_of_work import SqlAlchemyUnitOfWork
from app.domain.errors import BusinessRuleViolation, Forbidden, NotFound, StateConflict
from app.models.enums import ActorType, EventType, Role
from app.models.ticket import Ticket
from app.models.ticket_event import TicketEvent


class AssignmentService:
    def __init__(self, uow: SqlAlchemyUnitOfWork):
        self.uow = uow

    async def assign_ticket(
        self, principal: Principal, ticket_id: uuid.UUID, assignee_id: uuid.UUID
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

        # 4. Guarded update: allowed only while OPEN or IN_PROGRESS
        success = await self.uow.tickets.assign_if_open_or_in_progress(
            ticket_id=ticket_id, assignee_id=assignee_id
        )
        if not success:
            raise StateConflict("Ticket is in a terminal state or was modified concurrently")

        # 5. Emit ASSIGNMENT event
        event = TicketEvent(
            id=uuid.uuid4(),
            ticket_id=ticket_id,
            type=EventType.ASSIGNMENT,
            actor_type=ActorType.USER,
            actor_id=principal.id,
            created_at=now(),
        )
        self.uow.events.insert(event)

        # Fetch and return the updated ticket
        updated_ticket = await self.uow.tickets.get(ticket_id)
        assert updated_ticket is not None
        return updated_ticket
