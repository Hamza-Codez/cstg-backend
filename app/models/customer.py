from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.core import clock

from .base import Base, UUIDMixin
from .enums import CustomerTier


class Customer(Base, UUIDMixin):
    __tablename__ = "customer"

    email: Mapped[str] = mapped_column(CITEXT, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    tier: Mapped[CustomerTier] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=clock.now, nullable=False
    )
