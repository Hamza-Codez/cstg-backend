from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .enums import ActorType


class NotificationCursor(Base):
    """Where a principal has read up to (spec08 §3).

    The only thing notifications store. Everything else is derived at read time
    from *current* visibility, which is what makes an unassigned agent stop
    seeing a ticket's notifications immediately.
    """

    __tablename__ = "notification_cursor"

    principal_type: Mapped[ActorType] = mapped_column(primary_key=True)
    principal_id: Mapped[UUID] = mapped_column(primary_key=True)
    #: The **unread** boundary — what the badge counts from.
    last_read_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: The **visibility** floor, set by clear-all. NULL means never cleared,
    #: which is a different statement from "cleared at the epoch".
    #:
    #: Separate from `last_read_at` because reading and dismissing are different
    #: acts: marking read empties the badge, clearing empties the list.
    cleared_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint("principal_type <> 'SYSTEM'", name="cursor_principal_not_system"),
    )
