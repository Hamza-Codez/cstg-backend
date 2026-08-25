from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, String, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class AssignmentConfig(Base):
    """How new tickets are routed (spec07 §2).

    A singleton: the boolean primary key with ``CHECK (id)`` admits exactly one
    row. Chosen over a settings blob so the columns carry real types and real
    constraints, and so no code path can create a second config.
    """

    __tablename__ = "assignment_config"

    id: Mapped[bool] = mapped_column(Boolean, primary_key=True, server_default=text("true"))
    strategy: Mapped[str] = mapped_column(String(20), nullable=False, server_default="MANUAL")
    #: Ships false. Automation is opt-in, so the migration changes no behaviour.
    auto_assign_on_create: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_by: Mapped[UUID | None] = mapped_column(ForeignKey("app_user.id"), nullable=True)

    __table_args__ = (
        CheckConstraint("id", name="assignment_config_singleton"),
        CheckConstraint(
            "strategy IN ('MANUAL', 'ROUND_ROBIN', 'LEAST_LOADED')",
            name="assignment_config_strategy_valid",
        ),
    )
