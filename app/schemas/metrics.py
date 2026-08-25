import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.models.enums import Category, CustomerTier, Priority


class GroupMetrics(BaseModel):
    """One slice of the ticket population, by priority, tier or category."""

    open: int
    in_progress: int
    #: Added because after P16 the four v1 statuses no longer sum to the total,
    #: and a dashboard whose numbers do not add up is one nobody trusts.
    pending_customer: int
    resolved: int
    closed: int
    breached_open: int
    breach_rate: float
    #: Wall clock — what the customer experienced.
    avg_resolution_seconds: int
    #: Pause-excluded — what the desk actually spent.
    avg_working_seconds: int
    sla_met_rate: float

    model_config = ConfigDict(from_attributes=True)


#: Kept as the historical name for the by-priority slice.
PriorityMetrics = GroupMetrics


class MetricsOverview(BaseModel):
    open: int
    in_progress: int
    pending_customer: int
    resolved: int
    closed: int
    breached_open: int
    breach_rate: float
    avg_resolution_seconds: int
    avg_working_seconds: int
    sla_met_rate: float
    #: Every enum member is present even at zero, so the dashboard never has to
    #: branch on a missing key.
    by_priority: dict[Priority, GroupMetrics]
    by_tier: dict[CustomerTier, GroupMetrics]
    by_category: dict[Category, GroupMetrics]

    model_config = ConfigDict(from_attributes=True)


#: Named so the router, the service and the response model all constrain the
#: same set — a new bucket must be added in one place, not three.
Bucket = Literal["day", "week", "month"]
Metric = Literal["created", "resolved", "breached", "breach_rate"]


class TimeseriesPoint(BaseModel):
    bucket_start: datetime
    value: float


class TimeseriesResponse(BaseModel):
    """Empty buckets are emitted as zero, never omitted.

    A chart that silently drops a quiet day draws a straight line through the
    gap and misreports it as steady activity (spec09 §3).
    """

    bucket: Bucket
    metric: Metric
    #: Echoed back so a chart can label its axis from the response alone,
    #: without re-parsing the query string it sent.
    range_from: datetime
    range_to: datetime
    points: list[TimeseriesPoint]


class AgentSummary(BaseModel):
    id: uuid.UUID
    name: str
    is_active: bool


class AgentMetrics(BaseModel):
    agent: AgentSummary
    open_tickets: int
    in_progress: int
    pending_customer: int
    resolved_in_period: int
    sla_met_rate: float
    avg_working_seconds: int
    #: Live workload against the agent's own ceiling. **None means no ceiling**
    #: — deliberately not 0.0, which a bar chart would render as "idle".
    current_load_pct: float | None


class AgentMetricsResponse(BaseModel):
    items: list[AgentMetrics]
    #: Shipped in the payload, not only in the docs: an unlabelled
    #: approximation in a performance metric is worse than no metric, because
    #: someone will manage against it (spec09 §4).
    attribution_note: str = (
        "Tickets count toward whoever is assigned to them now. Reassigned tickets "
        "count entirely toward their current owner."
    )
