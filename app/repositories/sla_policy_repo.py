import uuid
from collections.abc import Sequence
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import Priority
from app.models.sla_policy import SlaPolicyEntry, SlaPolicyVersion


class SlaPolicyRepository:
    """Read and append SLA policy versions. Never updates entries."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def active_version(self) -> SlaPolicyVersion | None:
        stmt = select(SlaPolicyVersion).where(
            SlaPolicyVersion.activated_at.is_not(None),
            SlaPolicyVersion.superseded_at.is_(None),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def entries_for(self, version_id: uuid.UUID) -> dict[Priority, timedelta]:
        """A version's durations, shaped for `domain.sla`."""
        stmt = select(SlaPolicyEntry).where(SlaPolicyEntry.version_id == version_id)
        rows = (await self.session.execute(stmt)).scalars().all()
        return {row.priority: timedelta(seconds=row.seconds) for row in rows}

    async def list_versions(self) -> Sequence[SlaPolicyVersion]:
        """Newest first — history is read to answer 'why did this ticket get 6h?'."""
        stmt = select(SlaPolicyVersion).order_by(SlaPolicyVersion.created_at.desc())
        return (await self.session.execute(stmt)).scalars().all()

    async def all_entries(self) -> Sequence[SlaPolicyEntry]:
        return (await self.session.execute(select(SlaPolicyEntry))).scalars().all()

    def insert_version(self, version: SlaPolicyVersion) -> None:
        self.session.add(version)

    def insert_entry(self, entry: SlaPolicyEntry) -> None:
        self.session.add(entry)

    async def supersede(self, version_id: uuid.UUID, at: object) -> None:
        """Stamp the outgoing version.

        This is the one UPDATE on the table, and it touches only `superseded_at`
        — the durations themselves stay immutable, which is what makes an old
        version still able to explain an old ticket.
        """
        await self.session.execute(
            update(SlaPolicyVersion)
            .where(SlaPolicyVersion.id == version_id)
            .values(superseded_at=at)
        )
