import uuid
from collections.abc import Sequence

from app.core.authorization import Principal, authorize_ticket_access
from app.core.clock import now
from app.core.unit_of_work import SqlAlchemyUnitOfWork
from app.domain.errors import Forbidden, NotFound
from app.models.comment import Comment
from app.models.enums import ActorType, EventType, Role
from app.models.ticket_event import TicketEvent
from app.schemas.comment import CommentCreate


class CommentService:
    def __init__(self, uow: SqlAlchemyUnitOfWork):
        self.uow = uow

    async def add_comment(
        self, principal: Principal, ticket_id: uuid.UUID, cmd: CommentCreate
    ) -> Comment:
        """
        Adds a comment to a ticket.
        Only staff members (AGENT, DISPATCHER, ADMIN) are allowed to author comments.
        """
        if principal.role == Role.CUSTOMER:
            raise Forbidden("Customers cannot author comments")

        async with self.uow:
            # Check ticket existence
            ticket = await self.uow.tickets.get(ticket_id)
            if not ticket:
                raise NotFound("Ticket not found")

            # Note: For v1, staff can comment on any ticket they can see.
            # (Which is all tickets for DISPATCHER/ADMIN, and assigned for AGENTS.)
            if principal.role == Role.AGENT and ticket.assignee_id != principal.id:
                raise NotFound("Ticket not found")

            comment = Comment(
                ticket_id=ticket.id,
                author_id=principal.id,
                type=cmd.type,
                body=cmd.body,
                created_at=now(),
            )
            self.uow.comments.insert(comment)

            # Audit event
            event = TicketEvent(
                id=uuid.uuid4(),
                ticket_id=ticket.id,
                type=EventType.COMMENT,
                actor_type=ActorType.USER,
                actor_id=principal.id,
                detail={"type": cmd.type, "comment_id": str(comment.id)},
                created_at=now(),
            )
            self.uow.events.insert(event)

            return comment

    async def get_ticket_comments(
        self, principal: Principal, ticket_id: uuid.UUID
    ) -> Sequence[Comment]:
        """
        Gets scoped comments for a ticket.
        Customers can only see PUBLIC_REPLY. Staff can see all.
        """
        async with self.uow:
            ticket = await self.uow.tickets.get(ticket_id)
            if not ticket:
                raise NotFound("Ticket not found")

            # Object-level visibility, shared with every other read path
            # (AUTHORIZATION.md §4). Hidden resources surface as 404, not 403.
            authorize_ticket_access(principal, ticket)

            is_customer = principal.role == Role.CUSTOMER
            return await self.uow.comments.list_for_ticket(ticket_id, is_customer)
