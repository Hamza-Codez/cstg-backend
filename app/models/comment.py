import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core import clock

from .base import Base, UUIDMixin
from .enums import CommentType


class Comment(Base, UUIDMixin):
    __tablename__ = "comment"

    ticket_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ticket.id"), nullable=False)
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    type: Mapped[CommentType] = mapped_column(nullable=False)
    body: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=clock.now, nullable=False
    )

    __table_args__ = (Index("ix_comment_ticket", "ticket_id", "created_at"),)
