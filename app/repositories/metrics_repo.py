"""Aggregate reads for admin reporting (docs/API.md §9).

Data access only — no business rules. The repository returns raw per-priority
aggregates and lets ``metrics_service`` derive rates and averages, so the SQL stays
a single grouped pass and the arithmetic stays testable without a database.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import case, func, literal, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
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
    pending_customer: int
    breached_open: int
    breached_total: int
    #: Wall clock, for customer experience.
    resolution_seconds_sum: float
    #: Pause-excluded, for agent performance. The two answer different
    #: questions and neither substitutes for the other (spec09 §2).
    working_seconds_sum: float
    resolution_count: int
    sla_met_count: int


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

        # Working time excludes the pause. Reporting raw wall clock after
        # building a pause feature would mean the pause improves the SLA number
        # while making handling time look WORSE — a metric contradicting the
        # feature it measures (spec09 §2). Clamped at zero so a clock skew can
        # never produce negative work.
        working_seconds = case(
            (
                Ticket.resolved_at.is_not(None),
                func.greatest(
                    func.extract("epoch", Ticket.resolved_at - Ticket.created_at)
                    - Ticket.sla_paused_seconds,
                    0,
                ),
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
            _count_if(Ticket.status == TicketStatus.PENDING_CUSTOMER).label("pending_customer"),
            # PENDING_CUSTOMER is excluded from "breached and still open": the
            # clock is stopped, so the desk is not currently late on it.
            _count_if(
                Ticket.sla_breached_at.is_not(None)
                & Ticket.status.in_([TicketStatus.OPEN, TicketStatus.IN_PROGRESS])
            ).label("breached_open"),
            _count_if(Ticket.sla_breached_at.is_not(None)).label("breached_total"),
            func.sum(resolution_seconds).label("resolution_seconds_sum"),
            func.sum(working_seconds).label("working_seconds_sum"),
            func.count(Ticket.resolved_at).label("resolution_count"),
            # SLA met is judged against sla_due_at, not deadline — a ticket that
            # legitimately waited on its customer gets that time back.
            _count_if(
                Ticket.resolved_at.is_not(None) & (Ticket.resolved_at <= Ticket.sla_due_at)
            ).label("sla_met_count"),
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
                pending_customer=_as_int(row.pending_customer),
                breached_open=_as_int(row.breached_open),
                breached_total=_as_int(row.breached_total),
                resolution_seconds_sum=_as_float(row.resolution_seconds_sum),
                working_seconds_sum=_as_float(row.working_seconds_sum),
                resolution_count=_as_int(row.resolution_count),
                sla_met_count=_as_int(row.sla_met_count),
            )
            for row in rows
        ]

    async def aggregate_by(
        self, column: InstrumentedAttribute[Any]
    ) -> list[tuple[Any, dict[str, Any]]]:
        """The same grouped scan over any dimension (tier, category).

        Tier needs a join to `customer`; the caller supplies the column, so the
        aggregate expressions stay defined once.
        """
        from app.models.customer import Customer

        def _count_if(condition: ColumnElement[bool]) -> ColumnElement[int]:
            return func.sum(case((condition, 1), else_=0))

        resolution_seconds = case(
            (
                Ticket.resolved_at.is_not(None),
                func.extract("epoch", Ticket.resolved_at - Ticket.created_at),
            ),
            else_=None,
        )
        working_seconds = case(
            (
                Ticket.resolved_at.is_not(None),
                func.greatest(
                    func.extract("epoch", Ticket.resolved_at - Ticket.created_at)
                    - Ticket.sla_paused_seconds,
                    0,
                ),
            ),
            else_=None,
        )

        stmt = select(
            column.label("bucket"),
            func.count(Ticket.id).label("total"),
            _count_if(Ticket.status == TicketStatus.OPEN).label("open"),
            _count_if(Ticket.status == TicketStatus.IN_PROGRESS).label("in_progress"),
            _count_if(Ticket.status == TicketStatus.PENDING_CUSTOMER).label("pending_customer"),
            _count_if(Ticket.status == TicketStatus.RESOLVED).label("resolved"),
            _count_if(Ticket.status == TicketStatus.CLOSED).label("closed"),
            _count_if(
                Ticket.sla_breached_at.is_not(None)
                & Ticket.status.in_([TicketStatus.OPEN, TicketStatus.IN_PROGRESS])
            ).label("breached_open"),
            _count_if(Ticket.sla_breached_at.is_not(None)).label("breached_total"),
            func.sum(resolution_seconds).label("resolution_seconds_sum"),
            func.sum(working_seconds).label("working_seconds_sum"),
            func.count(Ticket.resolved_at).label("resolution_count"),
            _count_if(
                Ticket.resolved_at.is_not(None) & (Ticket.resolved_at <= Ticket.sla_due_at)
            ).label("sla_met_count"),
        ).group_by(column)

        if column is Customer.tier:
            stmt = stmt.join(Customer, Customer.id == Ticket.customer_id)

        rows = (await self.session.execute(stmt)).all()
        return [
            (
                row.bucket,
                {
                    "total": _as_int(row.total),
                    "open": _as_int(row.open),
                    "in_progress": _as_int(row.in_progress),
                    "pending_customer": _as_int(row.pending_customer),
                    "resolved": _as_int(row.resolved),
                    "closed": _as_int(row.closed),
                    "breached_open": _as_int(row.breached_open),
                    "breached_total": _as_int(row.breached_total),
                    "resolution_seconds_sum": _as_float(row.resolution_seconds_sum),
                    "working_seconds_sum": _as_float(row.working_seconds_sum),
                    "resolution_count": _as_int(row.resolution_count),
                    "sla_met_count": _as_int(row.sla_met_count),
                },
            )
            for row in rows
        ]

    async def timeseries(
        self, metric: str, bucket: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, float]]:
        """Bucketed counts with EMPTY BUCKETS AS ZERO.

        generate_series left-joined to the aggregate, so a quiet day is a point
        at zero rather than a gap the chart interpolates across (spec09 section 3).

        All bucketing is UTC, matching the timestamptz storage. Timezone-aware
        bucketing is out of scope, stated here so it is a known limitation.
        """
        column = {
            "created": Ticket.created_at,
            "resolved": Ticket.resolved_at,
            "breached": Ticket.sla_breached_at,
            "breach_rate": Ticket.created_at,
        }[metric]

        step = text("'1 " + bucket + "'::interval")
        series = select(
            func.generate_series(
                func.date_trunc(bucket, literal(start)),
                func.date_trunc(bucket, literal(end)),
                step,
            ).label("bucket_start")
        ).subquery()

        truncated = func.date_trunc(bucket, column)
        value: ColumnElement[Any]
        if metric == "breach_rate":
            value = case(
                (func.count(Ticket.id) == 0, 0.0),
                else_=func.sum(case((Ticket.sla_breached_at.is_not(None), 1.0), else_=0.0))
                / func.count(Ticket.id),
            )
        else:
            value = func.count(Ticket.id)

        grouped = (
            select(truncated.label("bucket_start"), value.label("value"))
            .where(column.is_not(None), column >= start, column < end)
            .group_by(truncated)
            .subquery()
        )

        stmt = (
            select(series.c.bucket_start, func.coalesce(grouped.c.value, 0).label("value"))
            .select_from(
                series.outerjoin(grouped, grouped.c.bucket_start == series.c.bucket_start)
            )
            .order_by(series.c.bucket_start)
        )

        rows = (await self.session.execute(stmt)).all()
        return [(row.bucket_start, float(row.value)) for row in rows]

    async def by_agent(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Per-agent workload and performance.

        Attribution is by CURRENT assignee. True custody-window attribution
        would mean reconstructing ownership from ASSIGNMENT events and
        apportioning time between holders -- real work, out of scope, and
        documented in the response rather than left implicit (spec09 section 4).

        Inactive agents holding tickets in range are INCLUDED, or the period's
        totals stop reconciling with the overview.
        """
        from app.models.user import AppUser

        def _count_if(condition: ColumnElement[bool]) -> ColumnElement[int]:
            return func.sum(case((condition, 1), else_=0))

        resolved_in_period = (
            Ticket.resolved_at.is_not(None)
            & (Ticket.resolved_at >= start)
            & (Ticket.resolved_at < end)
        )

        working_seconds = case(
            (
                resolved_in_period,
                func.greatest(
                    func.extract("epoch", Ticket.resolved_at - Ticket.created_at)
                    - Ticket.sla_paused_seconds,
                    0,
                ),
            ),
            else_=None,
        )

        stmt = (
            select(
                AppUser.id,
                AppUser.name,
                AppUser.is_active,
                # Carried through so the service can express load as a fraction
                # of the agent's own ceiling; NULL means no ceiling.
                AppUser.max_open_tickets,
                _count_if(Ticket.status == TicketStatus.OPEN).label("open_tickets"),
                _count_if(Ticket.status == TicketStatus.IN_PROGRESS).label("in_progress"),
                _count_if(Ticket.status == TicketStatus.PENDING_CUSTOMER).label(
                    "pending_customer"
                ),
                _count_if(resolved_in_period).label("resolved_in_period"),
                _count_if(resolved_in_period & (Ticket.resolved_at <= Ticket.sla_due_at)).label(
                    "sla_met"
                ),
                func.sum(working_seconds).label("working_seconds_sum"),
                func.count(case((resolved_in_period, 1), else_=None)).label("working_count"),
            )
            .join(Ticket, Ticket.assignee_id == AppUser.id)
            .group_by(AppUser.id, AppUser.name, AppUser.is_active, AppUser.max_open_tickets)
            .order_by(AppUser.name)
        )

        rows = (await self.session.execute(stmt)).all()
        return [
            {
                "id": row.id,
                "name": row.name,
                "is_active": row.is_active,
                "max_open_tickets": row.max_open_tickets,
                "open_tickets": _as_int(row.open_tickets),
                "in_progress": _as_int(row.in_progress),
                "pending_customer": _as_int(row.pending_customer),
                "resolved_in_period": _as_int(row.resolved_in_period),
                "sla_met": _as_int(row.sla_met),
                "working_seconds_sum": _as_float(row.working_seconds_sum),
                "working_count": _as_int(row.working_count),
            }
            for row in rows
        ]
