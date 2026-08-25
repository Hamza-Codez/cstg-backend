from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .enums import ActorType


class NotificationDismissal(Base):
    """One person hiding one notification from their own feed.

    `ticket_event` is append-only (migration 0006), so a dismissal can never be
    a delete — and should not be. The event is the audit log; one person tidying
    their inbox must not erase a record the whole system depends on. This table
    is the only thing that changes, and it changes nothing anyone else sees.

    Bulk clearing does *not* write rows here — that is
    `NotificationCursor.cleared_before`, so a clear stays one UPDATE instead of
    an unbounded insert. Rows at or below that timestamp are redundant and get
    pruned when it moves.
    """

    __tablename__ = "notification_dismissal"

    principal_type: Mapped[ActorType] = mapped_column(primary_key=True)
    # No foreign key: this points into `customer` or `app_user` depending on
    # principal_type — the polymorphism ticket_event.actor_id already lives with.
    principal_id: Mapped[UUID] = mapped_column(primary_key=True)
    # This one does get a foreign key: it points at exactly one table, and a
    # dismissal of an event that does not exist is meaningless.
    event_id: Mapped[UUID] = mapped_column(
        ForeignKey("ticket_event.id", ondelete="CASCADE"), primary_key=True
    )
    dismissed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint("principal_type <> 'SYSTEM'", name="dismissal_principal_not_system"),
    )
