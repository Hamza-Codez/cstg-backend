"""Staff management (docs/API.md §10, FRONTEND.md §7.4.3). Admin-only."""

import uuid

from app.core.clock import now
from app.core.security import get_password_hash
from app.core.unit_of_work import SqlAlchemyUnitOfWork
from app.domain.errors import BusinessRuleViolation, NotFound
from app.models.user import AppUser
from app.schemas.user import UserCreate, UserUpdate


class UserService:
    def __init__(self, uow: SqlAlchemyUnitOfWork):
        self.uow = uow

    async def create(self, data: UserCreate) -> AppUser:
        # `UserCreate.role` is a Literal that excludes CUSTOMER, so a customer role
        # cannot reach here — mypy proves it, and the request is rejected as a 400
        # by schema validation before the service is called.
        existing = await self.uow.users.get_by_email(data.email)
        if existing is not None:
            raise BusinessRuleViolation("That email address is already in use")

        user = AppUser(
            email=data.email,
            password_hash=get_password_hash(data.password),
            name=data.name,
            role=data.role,
            is_active=True,
            created_at=now(),
        )
        self.uow.users.insert(user)
        return user

    async def set_active(self, user_id: uuid.UUID, data: UserUpdate) -> AppUser:
        user = await self.uow.users.get(user_id)
        if user is None:
            raise NotFound("User not found")

        user.is_active = data.is_active
        return user
