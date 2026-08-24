"""Configuration contract (docs/API.md §11, FRONTEND.md §7.4.2)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import Category, CustomerTier, Priority


class PriorityRuleEntry(BaseModel):
    tier: CustomerTier
    category: Category
    priority: Priority


class SlaDurationEntry(BaseModel):
    priority: Priority
    seconds: int


class SlaPolicySummary(BaseModel):
    """The active policy — configurable from P17 (was read-only in v1)."""

    version_id: uuid.UUID
    activated_at: datetime | None
    note: str | None
    durations: list[SlaDurationEntry]


class SlaPolicyVersionSummary(SlaPolicySummary):
    """One entry of the history that makes a frozen deadline explainable."""

    created_at: datetime
    superseded_at: datetime | None
    is_active: bool


class ConfigurationResponse(BaseModel):
    priority_rules: list[PriorityRuleEntry]
    #: Retained for compatibility: the active policy's durations, flat.
    sla_durations: list[SlaDurationEntry]
    sla_policy: SlaPolicySummary


class SlaPolicyUpdate(BaseModel):
    """A full replacement of the durations.

    Whole-policy rather than per-priority for the same reason as the matrix: the
    mapping must stay **total** (SLA_ENGINE.md §2), and per-priority edits pass
    through states that are not.
    """

    durations: list[SlaDurationEntry]
    note: str | None = Field(default=None, max_length=200)


class PriorityMatrixUpdate(BaseModel):
    """A full replacement of the matrix.

    Whole-matrix rather than per-cell on purpose: the mapping must stay **total**
    (SLA_ENGINE.md §2), and a sequence of per-cell edits has intermediate states
    that are not. Sending the entire grid lets the server validate completeness in
    one transaction.
    """

    rules: list[PriorityRuleEntry]
