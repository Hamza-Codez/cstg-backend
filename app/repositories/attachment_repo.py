import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attachment import Attachment


class AttachmentRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    def insert(self, attachment: Attachment) -> None:
        self.session.add(attachment)

    async def get(self, attachment_id: uuid.UUID) -> Attachment | None:
        stmt = select(Attachment).where(Attachment.id == attachment_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_ticket(self, ticket_id: uuid.UUID) -> Sequence[Attachment]:
        stmt = (
            select(Attachment)
            .where(Attachment.ticket_id == ticket_id)
            .order_by(Attachment.created_at)
        )
        return (await self.session.execute(stmt)).scalars().all()
