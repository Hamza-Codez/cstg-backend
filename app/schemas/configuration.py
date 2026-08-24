"""Configuration contract (docs/API.md §11, FRONTEND.md §7.4.2)."""

from pydantic import BaseModel

from app.models.enums import Category, CustomerTier, Priority


class PriorityRuleEntry(BaseModel):
    tier: CustomerTier
    category: Category
    priority: Priority


class SlaDurationEntry(BaseModel):
    """Read-only in v1 — the durations are fixed in the domain (SLA_ENGINE.md §1)."""

    priority: Priority
    seconds: int


class ConfigurationResponse(BaseModel):
    priority_rules: list[PriorityRuleEntry]
    sla_durations: list[SlaDurationEntry]


class PriorityMatrixUpdate(BaseModel):
    """A full replacement of the matrix.

    Whole-matrix rather than per-cell on purpose: the mapping must stay **total**
    (SLA_ENGINE.md §2), and a sequence of per-cell edits has intermediate states
    that are not. Sending the entire grid lets the server validate completeness in
    one transaction.
    """

    rules: list[PriorityRuleEntry]
