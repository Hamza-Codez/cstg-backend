import typing
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core import clock

from .base import Base, UUIDMixin
from .enums import CommentType


class Comment(Base, UUIDMixin):
    __tablename__ = "comment"

    ticket_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ticket.id"), nullable=False)
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id"))
    author_customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customer.id"))
    type: Mapped[CommentType] = mapped_column(nullable=False)
    body: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=clock.now, nullable=False
    )

    user = relationship("AppUser", foreign_keys=[author_user_id], lazy="noload")
    customer = relationship("Customer", foreign_keys=[author_customer_id], lazy="noload")

    @property
    def author(self) -> dict[str, typing.Any]:
        if self.author_user_id:
            user = self.__dict__.get("user")
            return {
                "type": "USER",
                "id": self.author_user_id,
                "name": user.name if user else "",
            }
        customer = self.__dict__.get("customer")
        return {
            "type": "CUSTOMER",
            "id": self.author_customer_id,
            "name": customer.name if customer else "",
        }

    __table_args__ = (
        Index("ix_comment_ticket", "ticket_id", "created_at"),
        CheckConstraint(
            "(author_user_id IS NULL) <> (author_customer_id IS NULL)",
            name="comment_exactly_one_author",
        ),
        CheckConstraint(
            "author_customer_id IS NULL OR type = 'PUBLIC_REPLY'",
            name="comment_customer_public_only",
        ),
    )
