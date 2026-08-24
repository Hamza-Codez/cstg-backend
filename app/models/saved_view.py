import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core import clock

from .base import Base, UUIDMixin


class SavedView(Base, UUIDMixin):
    """A named filter combination owned by one staff user (spec04 §6)."""

    __tablename__ = "saved_view"

    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    # Stored as the same shape TicketFilters serialises to, and re-validated
    # against the owner's *current* role on execution — roles change.
    filters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=clock.now, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_saved_view_owner_name"),
        Index("ix_saved_view_owner", "owner_id", text("created_at")),
    )
