from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .enums import Category, CustomerTier, Priority


class PriorityRule(Base):
    __tablename__ = "priority_rule"

    tier: Mapped[CustomerTier] = mapped_column(primary_key=True)
    category: Mapped[Category] = mapped_column(primary_key=True)
    priority: Mapped[Priority] = mapped_column(nullable=False)
