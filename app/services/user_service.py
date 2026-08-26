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

    async def update_staff(self, user_id: uuid.UUID, data: UserUpdate) -> AppUser:
        """Partial update: activation, capacity, automation opt-out.

        Only fields present in the request are touched, so changing a ceiling
        cannot accidentally reactivate someone.

        Deactivating still never removes them and never reassigns their existing
        work — that is a dispatcher decision (docs/API.md §10).
        """
        user = await self.uow.users.get(user_id)
        if user is None:
            raise NotFound("User not found")

        fields = data.model_dump(exclude_unset=True)
        if "is_active" in fields and fields["is_active"] is not None:
            user.is_active = fields["is_active"]
        if "max_open_tickets" in fields:
            # Explicit null clears the ceiling — "no limit" is a real setting.
            user.max_open_tickets = fields["max_open_tickets"]
        if "accepts_auto_assignment" in fields and fields["accepts_auto_assignment"] is not None:
            user.accepts_auto_assignment = fields["accepts_auto_assignment"]
        return user

    async def delete_staff(self, actor_id: uuid.UUID, user_id: uuid.UUID) -> None:
        """Delete a staff row that has never been used.

        **Deletion is not the counterpart of deactivation.** Deactivating keeps
        the person's history intact and stops them receiving new work; that is
        the right answer for someone who has left. Deleting is for the row
        created by a typo five minutes ago — and only that.

        Anyone who has held a ticket, written a comment, published an SLA policy
        or authored a single audit event is refused, because removing them would
        leave those records attributed to an id that resolves to nobody.
        `ticket_event.actor_id` has no foreign key, so the database will not
        stop this; the check has to.
        """
        if actor_id == user_id:
            # Not a rule about data, a rule about lockout: an admin deleting
            # their own row loses the session and, if they were the last admin,
            # the only way back in.
            raise BusinessRuleViolation("You cannot delete your own account.")

        user = await self.uow.users.get(user_id)
        if user is None:
            raise NotFound("That staff member could not be found.")

        footprint = await self.uow.users.footprint(user_id)
        used = {name: n for name, n in footprint.items() if n > 0}
        if used:
            detail = ", ".join(f"{n} {name.replace('_', ' ')}" for name, n in sorted(used.items()))
            raise BusinessRuleViolation(
                f"{user.name} has already worked in the system ({detail}), so deleting them "
                "would leave that history attributed to nobody. Deactivate them instead — "
                "they stop receiving new work and their record stays intact."
            )

        await self.uow.users.delete(user)
