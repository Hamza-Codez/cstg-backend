import uuid
from collections.abc import Sequence

from app.core.authorization import Principal, authorize_ticket_access
from app.core.clock import now
from app.core.unit_of_work import SqlAlchemyUnitOfWork
from app.domain.errors import NotFound
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
        """
        from app.core.authorization import authorize_comment_authoring
        from app.domain.errors import BusinessRuleViolation
        from app.models.enums import TicketStatus

        async with self.uow:
            # Check ticket existence
            ticket = await self.uow.tickets.get(ticket_id)
            if not ticket:
                raise NotFound("Ticket not found")

            if ticket.status == TicketStatus.CLOSED:
                raise BusinessRuleViolation("This request is closed.")

            authorize_comment_authoring(principal, ticket, cmd.type)

            if principal.type == ActorType.CUSTOMER:
                author_customer_id = principal.id
                author_user_id = None
                actor_type = ActorType.CUSTOMER
            else:
                author_customer_id = None
                author_user_id = principal.id
                actor_type = ActorType.USER

            comment = Comment(
                ticket_id=ticket.id,
                author_customer_id=author_customer_id,
                author_user_id=author_user_id,
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
                actor_type=actor_type,
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
