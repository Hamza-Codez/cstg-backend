import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core import clock

from .base import Base, UUIDMixin
from .enums import ActorType, EventType, TicketStatus


class TicketEvent(Base, UUIDMixin):
    __tablename__ = "ticket_event"

    ticket_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ticket.id"), nullable=False)
    type: Mapped[EventType] = mapped_column(nullable=False)
    actor_type: Mapped[ActorType] = mapped_column(nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    from_status: Mapped[TicketStatus | None] = mapped_column(nullable=True)
    to_status: Mapped[TicketStatus | None] = mapped_column(nullable=True)
    detail: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=clock.now, nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "(actor_type = 'SYSTEM') = (actor_id IS NULL)", name="system_actor_has_no_id"
        ),
        Index("ix_event_ticket", "ticket_id", "created_at"),
    )
