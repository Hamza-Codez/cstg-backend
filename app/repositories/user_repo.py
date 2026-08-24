import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import Role
from app.models.user import AppUser


class UserRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, user_id: uuid.UUID) -> AppUser | None:
        """Fetch a staff member by id regardless of role or active state.

        Deliberately unfiltered: a ticket assigned before its owner was
        deactivated must still be able to name that owner.
        """
        stmt = select(AppUser).where(AppUser.id == user_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_active_agent(self, user_id: uuid.UUID) -> AppUser | None:
        """Fetch a user only if they are an active AGENT."""
        stmt = select(AppUser).where(
            AppUser.id == user_id,
            AppUser.role == Role.AGENT,
            AppUser.is_active.is_(True),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_staff(
        self, *, role: Role | None = None, is_active: bool | None = None
    ) -> Sequence[AppUser]:
        """Staff directory, ordered by name for a predictable picker."""
        stmt = select(AppUser).order_by(AppUser.name)
        if role is not None:
            stmt = stmt.where(AppUser.role == role)
        if is_active is not None:
            stmt = stmt.where(AppUser.is_active.is_(is_active))
        return (await self.session.execute(stmt)).scalars().all()

    async def get_by_email(self, email: str) -> AppUser | None:
        """`email` is citext, so this comparison is case-insensitive."""
        stmt = select(AppUser).where(AppUser.email == email)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    def insert(self, user: AppUser) -> None:
        """Stage a new staff member; the caller's unit of work commits it."""
        self.session.add(user)
