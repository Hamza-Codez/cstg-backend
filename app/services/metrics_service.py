"""Admin reporting aggregates (docs/API.md §9, spec09).

Read-only: no transaction is opened and nothing is written, so this service takes
no unit of work. SQL lives in ``metrics_repo``; all arithmetic lives here, which
is what keeps the derivations testable without a database.
"""

import uuid
from datetime import datetime, timedelta
from typing import Any

from app.domain.errors import BusinessRuleViolation
from app.models.customer import Customer
from app.models.enums import Category, CustomerTier, Priority
from app.models.ticket import Ticket
from app.repositories.metrics_repo import MetricsRepository, PriorityAggregate
from app.schemas.metrics import (
    AgentMetrics,
    AgentMetricsResponse,
    AgentSummary,
    Bucket,
    GroupMetrics,
    Metric,
    MetricsOverview,
    TimeseriesPoint,
    TimeseriesResponse,
)

#: A client must not be able to ask for a decade of daily points.
MAX_BUCKETS = 366


def _rate(part: int, whole: int) -> float:
    return part / whole if whole else 0.0


def _average(total_seconds: float, count: int) -> int:
    return int(total_seconds / count) if count else 0


def _load(row: dict[str, Any]) -> float | None:
    """Live workload as a fraction of the agent's own ceiling.

    Returns None — not 0.0 — when the agent has no ceiling, because a bar chart
    renders 0.0 as "idle" and that would be a lie about someone holding forty
    tickets. Uncapped, not unloaded.

    The numerator matches `has_capacity`'s notion of "open": anything not yet
    resolved, `PENDING_CUSTOMER` included. A ticket waiting on a customer still
    occupies the agent even though its SLA clock is paused.
    """
    ceiling = row["max_open_tickets"]
    if ceiling is None or ceiling <= 0:
        return None
    held = row["open_tickets"] + row["in_progress"] + row["pending_customer"]
    return float(held) / float(ceiling)


def _metrics(data: dict[str, Any]) -> GroupMetrics:
    return GroupMetrics(
        open=data["open"],
        in_progress=data["in_progress"],
        pending_customer=data["pending_customer"],
        resolved=data["resolved"],
        closed=data["closed"],
        breached_open=data["breached_open"],
        breach_rate=_rate(data["breached_total"], data["total"]),
        avg_resolution_seconds=_average(data["resolution_seconds_sum"], data["resolution_count"]),
        avg_working_seconds=_average(data["working_seconds_sum"], data["resolution_count"]),
        sla_met_rate=_rate(data["sla_met_count"], data["resolution_count"]),
    )


def _empty() -> GroupMetrics:
    return _metrics(
        {
            "total": 0,
            "open": 0,
            "in_progress": 0,
            "pending_customer": 0,
            "resolved": 0,
            "closed": 0,
            "breached_open": 0,
            "breached_total": 0,
            "resolution_seconds_sum": 0.0,
            "working_seconds_sum": 0.0,
            "resolution_count": 0,
            "sla_met_count": 0,
        }
    )


def _as_dict(aggregate: PriorityAggregate) -> dict[str, Any]:
    return {
        "total": aggregate.total,
        "open": aggregate.open,
        "in_progress": aggregate.in_progress,
        "pending_customer": aggregate.pending_customer,
        "resolved": aggregate.resolved,
        "closed": aggregate.closed,
        "breached_open": aggregate.breached_open,
        "breached_total": aggregate.breached_total,
        "resolution_seconds_sum": aggregate.resolution_seconds_sum,
        "working_seconds_sum": aggregate.working_seconds_sum,
        "resolution_count": aggregate.resolution_count,
        "sla_met_count": aggregate.sla_met_count,
    }


class MetricsService:
    def __init__(self, repo: MetricsRepository) -> None:
        self.repo = repo

    async def get_overview_metrics(self) -> MetricsOverview:
        aggregates = await self.repo.aggregate_by_priority()

        by_priority = {a.priority: _metrics(_as_dict(a)) for a in aggregates}
        # Every enum member is present even with no tickets, so the dashboard's
        # shape is stable rather than varying with the data.
        for priority in Priority:
            by_priority.setdefault(priority, _empty())

        by_tier: dict[CustomerTier, GroupMetrics] = {
            CustomerTier(bucket): _metrics(data)
            for bucket, data in await self.repo.aggregate_by(Customer.tier)
        }
        for tier in CustomerTier:
            by_tier.setdefault(tier, _empty())

        by_category: dict[Category, GroupMetrics] = {
            Category(bucket): _metrics(data)
            for bucket, data in await self.repo.aggregate_by(Ticket.category)
        }
        for category in Category:
            by_category.setdefault(category, _empty())

        total = sum(a.total for a in aggregates)
        resolution_count = sum(a.resolution_count for a in aggregates)

        return MetricsOverview(
            open=sum(a.open for a in aggregates),
            in_progress=sum(a.in_progress for a in aggregates),
            # Added at P16: without it the four v1 statuses no longer sum to the
            # total, and a dashboard whose numbers do not add up is one nobody
            # trusts.
            pending_customer=sum(a.pending_customer for a in aggregates),
            resolved=sum(a.resolved for a in aggregates),
            closed=sum(a.closed for a in aggregates),
            breached_open=sum(a.breached_open for a in aggregates),
            breach_rate=_rate(sum(a.breached_total for a in aggregates), total),
            # Summed across priorities, not averaged from per-priority averages —
            # the latter is only correct when every group is the same size.
            avg_resolution_seconds=_average(
                sum(a.resolution_seconds_sum for a in aggregates), resolution_count
            ),
            avg_working_seconds=_average(
                sum(a.working_seconds_sum for a in aggregates), resolution_count
            ),
            sla_met_rate=_rate(sum(a.sla_met_count for a in aggregates), resolution_count),
            by_priority=by_priority,
            by_tier=by_tier,
            by_category=by_category,
        )

    async def timeseries(
        self, metric: Metric, bucket: Bucket, start: datetime, end: datetime
    ) -> TimeseriesResponse:
        """Bucketed history. Over MAX_BUCKETS the range is refused, not truncated."""
        if end <= start:
            raise BusinessRuleViolation("The end of the range must be after its start.")

        # A month is not a fixed span, so 28 days is the conservative divisor:
        # it over-counts buckets slightly and can only ever refuse a range that
        # was borderline, never admit one that is genuinely too long.
        span = {"day": timedelta(days=1), "week": timedelta(weeks=1), "month": timedelta(days=28)}
        if (end - start) / span[bucket] > MAX_BUCKETS:
            raise BusinessRuleViolation(
                f"That range is more than {MAX_BUCKETS} {bucket} buckets. Narrow it, "
                "or choose a larger bucket."
            )

        points = await self.repo.timeseries(metric, bucket, start, end)
        return TimeseriesResponse(
            bucket=bucket,
            metric=metric,
            range_from=start,
            range_to=end,
            points=[TimeseriesPoint(bucket_start=at, value=value) for at, value in points],
        )

    async def by_agent(self, start: datetime, end: datetime) -> AgentMetricsResponse:
        rows = await self.repo.by_agent(start, end)
        return AgentMetricsResponse(
            items=[
                AgentMetrics(
                    agent=AgentSummary(
                        id=uuid.UUID(str(row["id"])),
                        name=row["name"],
                        is_active=row["is_active"],
                    ),
                    open_tickets=row["open_tickets"],
                    in_progress=row["in_progress"],
                    pending_customer=row["pending_customer"],
                    resolved_in_period=row["resolved_in_period"],
                    sla_met_rate=_rate(row["sla_met"], row["resolved_in_period"]),
                    avg_working_seconds=_average(row["working_seconds_sum"], row["working_count"]),
                    current_load_pct=_load(row),
                )
                for row in rows
            ]
        )
