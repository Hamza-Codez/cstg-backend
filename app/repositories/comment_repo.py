import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.comment import Comment
from app.models.enums import CommentType


class CommentRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    def insert(self, comment: Comment) -> None:
        self.session.add(comment)

    async def list_for_ticket(self, ticket_id: uuid.UUID, is_customer: bool) -> Sequence[Comment]:
        """
        Fetch comments for a ticket.
        If is_customer is True, only PUBLIC_REPLY comments are returned.
        """
        stmt = select(Comment).where(Comment.ticket_id == ticket_id).order_by(Comment.created_at)

        if is_customer:
            stmt = stmt.where(Comment.type == CommentType.PUBLIC_REPLY)

        return (await self.session.execute(stmt)).scalars().all()
