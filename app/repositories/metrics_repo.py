"""Aggregate reads for admin reporting (docs/API.md §9).

Data access only — no business rules. The repository returns raw per-priority
aggregates and lets ``metrics_service`` derive rates and averages, so the SQL stays
a single grouped pass and the arithmetic stays testable without a database.
"""

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.models.enums import Priority, TicketStatus
from app.models.ticket import Ticket


def _as_int(value: int | Decimal | None) -> int:
    return int(value) if value is not None else 0


def _as_float(value: float | Decimal | None) -> float:
    # PostgreSQL SUM/AVG over numeric returns Decimal; mixing it with float
    # arithmetic raises TypeError, so normalise at the boundary.
    return float(value) if value is not None else 0.0


@dataclass(frozen=True)
class PriorityAggregate:
    """One row of the grouped scan, already coerced out of Decimal."""

    priority: Priority
    total: int
    open: int
    in_progress: int
    resolved: int
    closed: int
    breached_open: int
    breached_total: int
    resolution_seconds_sum: float
    resolution_count: int


class MetricsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def aggregate_by_priority(self) -> list[PriorityAggregate]:
        """Counts, breach totals, and resolution time, grouped by priority.

        Returns SUM and COUNT rather than AVG so the service can compute an exact
        global average; averaging per-priority averages would only be correct by
        coincidence of equal group sizes.
        """

        def _count_if(condition: ColumnElement[bool]) -> ColumnElement[int]:
            """COUNT of rows matching a predicate, as a SUM over a 0/1 CASE."""
            return func.sum(case((condition, 1), else_=0))

        # A resolved_at is only ever set on RESOLVED/CLOSED (DATABASE.md §3
        # resolved_requires_status), so counting it counts resolution events.
        resolution_seconds = case(
            (
                Ticket.resolved_at.is_not(None),
                func.extract("epoch", Ticket.resolved_at - Ticket.created_at),
            ),
            else_=None,
        )

        stmt = select(
            Ticket.priority,
            func.count(Ticket.id).label("total"),
            _count_if(Ticket.status == TicketStatus.OPEN).label("open"),
            _count_if(Ticket.status == TicketStatus.IN_PROGRESS).label("in_progress"),
            _count_if(Ticket.status == TicketStatus.RESOLVED).label("resolved"),
            _count_if(Ticket.status == TicketStatus.CLOSED).label("closed"),
            _count_if(
                Ticket.sla_breached_at.is_not(None)
                & Ticket.status.in_([TicketStatus.OPEN, TicketStatus.IN_PROGRESS])
            ).label("breached_open"),
            _count_if(Ticket.sla_breached_at.is_not(None)).label("breached_total"),
            func.sum(resolution_seconds).label("resolution_seconds_sum"),
            func.count(Ticket.resolved_at).label("resolution_count"),
        ).group_by(Ticket.priority)

        rows = (await self.session.execute(stmt)).all()
        return [
            PriorityAggregate(
                priority=row.priority,
                total=_as_int(row.total),
                open=_as_int(row.open),
                in_progress=_as_int(row.in_progress),
                resolved=_as_int(row.resolved),
                closed=_as_int(row.closed),
                breached_open=_as_int(row.breached_open),
                breached_total=_as_int(row.breached_total),
                resolution_seconds_sum=_as_float(row.resolution_seconds_sum),
                resolution_count=_as_int(row.resolution_count),
            )
            for row in rows
        ]
