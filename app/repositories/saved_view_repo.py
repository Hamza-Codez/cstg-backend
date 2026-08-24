import uuid
from collections.abc import Sequence

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.saved_view import SavedView


class SavedViewRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    def insert(self, view: SavedView) -> None:
        self.session.add(view)

    async def list_for_owner(self, owner_id: uuid.UUID) -> Sequence[SavedView]:
        stmt = (
            select(SavedView)
            .where(SavedView.owner_id == owner_id)
            .order_by(SavedView.created_at)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def get_for_owner(
        self, view_id: uuid.UUID, owner_id: uuid.UUID
    ) -> SavedView | None:
        """Scoped by owner in the query itself.

        Fetching by id and comparing afterwards would work, but keeping the
        owner in the predicate means no call site can forget the check.
        """
        stmt = select(SavedView).where(
            SavedView.id == view_id, SavedView.owner_id == owner_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def delete_for_owner(self, view_id: uuid.UUID, owner_id: uuid.UUID) -> bool:
        """Guarded delete; False means it did not exist *for this owner*."""
        stmt = delete(SavedView).where(
            SavedView.id == view_id, SavedView.owner_id == owner_id
        )
        result = await self.session.execute(stmt)
        # Same narrowing the ticket repo uses for its guarded updates: only a
        # CursorResult carries rowcount.
        assert isinstance(result, CursorResult)
        return bool(result.rowcount)
