import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core import clock

from .base import Base, UUIDMixin


class Attachment(Base, UUIDMixin):
    __tablename__ = "attachment"

    ticket_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ticket.id"), nullable=False)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    content_type: Mapped[str] = mapped_column(String, nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_path: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=clock.now, nullable=False
    )

    __table_args__ = (Index("ix_attachment_ticket", "ticket_id", "created_at"),)
