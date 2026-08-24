"""Admin reporting aggregates (docs/API.md §9).

Read-only: no transaction is opened and nothing is written, so this service takes
no unit of work. All arithmetic happens here; the SQL lives in ``metrics_repo``.
"""

from app.models.enums import Priority
from app.repositories.metrics_repo import MetricsRepository, PriorityAggregate
from app.schemas.metrics import MetricsOverview, PriorityMetrics


def _rate(part: int, whole: int) -> float:
    return part / whole if whole else 0.0


def _average(total_seconds: float, count: int) -> int:
    return int(total_seconds / count) if count else 0


class MetricsService:
    def __init__(self, repo: MetricsRepository) -> None:
        self.repo = repo

    async def get_overview_metrics(self) -> MetricsOverview:
        aggregates = await self.repo.aggregate_by_priority()
        by_priority = {a.priority: self._to_metrics(a) for a in aggregates}

        # Every priority appears in the response even with no tickets, so the shape
        # is stable for the dashboard rather than varying with the data.
        for priority in Priority:
            by_priority.setdefault(priority, self._empty())

        return MetricsOverview(
            open=sum(a.open for a in aggregates),
            in_progress=sum(a.in_progress for a in aggregates),
            resolved=sum(a.resolved for a in aggregates),
            closed=sum(a.closed for a in aggregates),
            breached_open=sum(a.breached_open for a in aggregates),
            breach_rate=_rate(
                sum(a.breached_total for a in aggregates),
                sum(a.total for a in aggregates),
            ),
            # Summed across priorities, not averaged from per-priority averages —
            # the latter is only correct when every group is the same size.
            avg_resolution_seconds=_average(
                sum(a.resolution_seconds_sum for a in aggregates),
                sum(a.resolution_count for a in aggregates),
            ),
            by_priority=by_priority,
        )

    @staticmethod
    def _to_metrics(a: PriorityAggregate) -> PriorityMetrics:
        return PriorityMetrics(
            open=a.open,
            in_progress=a.in_progress,
            resolved=a.resolved,
            closed=a.closed,
            breached_open=a.breached_open,
            breach_rate=_rate(a.breached_total, a.total),
            avg_resolution_seconds=_average(a.resolution_seconds_sum, a.resolution_count),
        )

    @staticmethod
    def _empty() -> PriorityMetrics:
        return PriorityMetrics(
            open=0,
            in_progress=0,
            resolved=0,
            closed=0,
            breached_open=0,
            breach_rate=0.0,
            avg_resolution_seconds=0,
        )
