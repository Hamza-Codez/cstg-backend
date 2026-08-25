import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, UUIDMixin
from .enums import Priority


class SlaPolicyVersion(Base, UUIDMixin):
    """One immutable set of SLA durations (spec06 §3).

    Never updated, never deleted — editing a policy creates a new version. A
    record that explains a past decision has to still be there when someone
    asks, the same reasoning that makes ticket_event append-only.

    Exactly one row may be active at a time; the partial unique index
    ``ix_sla_policy_active`` (migration 0015) enforces it, so two concurrent
    activations resolve as a 409 rather than as two live policies.
    """

    __tablename__ = "sla_policy_version"

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    #: NULL for the seeded v1 policy — no admin authored it.
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id"), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: What this version is for, shown in history. The difference between a
    #: useful audit and a list of dates.
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)


class SlaPolicyEntry(Base):
    """One priority's duration within a version. Composite PK, no surrogate id."""

    __tablename__ = "sla_policy_entry"

    version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sla_policy_version.id", ondelete="CASCADE"), primary_key=True
    )
    priority: Mapped[Priority] = mapped_column(primary_key=True)
    seconds: Mapped[int] = mapped_column(Integer, nullable=False)
