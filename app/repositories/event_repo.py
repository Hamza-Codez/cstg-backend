import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ticket_event import TicketEvent


class TicketEventRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    def insert(self, event: TicketEvent) -> None:
        """Add an event to the current transaction. Append-only logic."""
        self.session.add(event)

    async def list_by_ticket(self, ticket_id: uuid.UUID) -> Sequence[TicketEvent]:
        """Fetch all events for a ticket, ordered by creation time."""
        stmt = (
            select(TicketEvent)
            .where(TicketEvent.ticket_id == ticket_id)
            .order_by(TicketEvent.created_at)
        )
        return (await self.session.execute(stmt)).scalars().all()
