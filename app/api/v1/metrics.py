import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_principal, get_db
from app.core.authorization import Principal, require_roles
from app.core.clock import now
from app.models.enums import Category, CustomerTier, Role, TicketStatus
from app.models.enums import Priority as TicketPriority
from app.repositories.metrics_repo import MetricsRepository
from app.repositories.ticket_repo import TicketRepository
from app.schemas.metrics import (
    AgentMetricsResponse,
    Bucket,
    Metric,
    MetricsOverview,
    TimeseriesResponse,
)
from app.schemas.ticket_filters import TicketFilters
from app.services.export_service import ExportService
from app.services.metrics_service import MetricsService

router = APIRouter(
    prefix="/metrics", tags=["Metrics"], dependencies=[Depends(get_current_principal)]
)

#: Metrics are management information, so `DISPATCHER` is denied alongside
#: `AGENT` and `CUSTOMER` (docs/AUTHORIZATION.md §3). Named once rather than
#: repeated per route, so a fourth endpoint cannot be added a notch looser.
admin_only = Depends(require_roles(Role.ADMIN))


async def get_metrics_service(session: Annotated[AsyncSession, Depends(get_db)]) -> MetricsService:
    return MetricsService(MetricsRepository(session))


async def get_export_service(session: Annotated[AsyncSession, Depends(get_db)]) -> ExportService:
    return ExportService(TicketRepository(session))


@router.get("/overview", response_model=MetricsOverview, dependencies=[admin_only])
async def get_overview(
    service: Annotated[MetricsService, Depends(get_metrics_service)],
) -> MetricsOverview:
    """Current-state metrics, globally and sliced by priority, tier and category.

    Every enum member is present even at zero, so a dashboard never has to
    branch on a missing key.
    """
    return await service.get_overview_metrics()


@router.get("/timeseries", response_model=TimeseriesResponse, dependencies=[admin_only])
async def get_timeseries(
    service: Annotated[MetricsService, Depends(get_metrics_service)],
    metric: Metric = "created",
    bucket: Bucket = "day",
    range_from: Annotated[datetime | None, Query(alias="from")] = None,
    range_to: Annotated[datetime | None, Query(alias="to")] = None,
) -> TimeseriesResponse:
    """Bucketed history, with **empty buckets emitted as zero**.

    A chart that silently omits a quiet day draws a straight line through the
    gap and misreports it as steady activity (spec09 §3).

    Bucketing is UTC, matching the `timestamptz` storage. Timezone-aware
    bucketing is out of scope; it is stated here so it is a known limitation
    rather than an assumed feature. A range over 366 buckets is refused with
    422 rather than truncated.
    """
    end = range_to or now()
    start = range_from or end - timedelta(days=30)
    return await service.timeseries(metric, bucket, start, end)


@router.get("/agents", response_model=AgentMetricsResponse, dependencies=[admin_only])
async def get_agent_metrics(
    service: Annotated[MetricsService, Depends(get_metrics_service)],
    range_from: Annotated[datetime | None, Query(alias="from")] = None,
    range_to: Annotated[datetime | None, Query(alias="to")] = None,
) -> AgentMetricsResponse:
    """Per-agent workload and performance over a period.

    **Attribution is by current assignee**, and the response says so in
    `attribution_note`. A reassigned ticket counts entirely toward whoever
    holds it now; true attribution would mean reconstructing custody windows
    from `ASSIGNMENT` events and apportioning time between holders. Shipping
    the caveat in the payload is deliberate — an unlabelled approximation in a
    performance metric is worse than no metric, because someone will manage
    against it.

    Inactive agents holding tickets in range are included, or the period's
    totals stop reconciling with the overview.
    """
    end = range_to or now()
    start = range_from or end - timedelta(days=30)
    return await service.by_agent(start, end)


@router.get(
    "/export/tickets.csv",
    dependencies=[admin_only],
    response_class=StreamingResponse,
    responses={200: {"content": {"text/csv": {}}, "description": "Streamed CSV."}},
)
async def export_tickets_csv(
    principal: Annotated[Principal, Depends(get_current_principal)],
    service: Annotated[ExportService, Depends(get_export_service)],
    q: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    status_filter: Annotated[TicketStatus | None, Query(alias="status")] = None,
    priority: TicketPriority | None = None,
    category: Category | None = None,
    breached: bool | None = None,
    assigned: bool | None = None,
    escalated: bool | None = None,
    tier: CustomerTier | None = None,
    assignee_id: uuid.UUID | None = None,
    customer_id: uuid.UUID | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
) -> StreamingResponse:
    """Export the ticket list as CSV.

    **The same filter set as `GET /tickets`**, built from the same
    `filter_predicates`, so an admin exports exactly the view they are looking
    at rather than learning a second filter language.

    Streamed over a server-side cursor: this process also hosts the SLA
    monitor, and materialising a large export would stall breach detection.
    Over `APP_EXPORT_MAX_ROWS` the request is refused with 422 — checked before
    the first byte, because a streamed response cannot be turned back into an
    error, and a truncated file that does not say so is a wrong answer.

    No ticket body and no comment text: operational data, not a content dump.
    """
    filters = TicketFilters(
        q=q,
        status=status_filter,
        priority=priority,
        category=category,
        breached=breached,
        assigned=assigned,
        escalated=escalated,
        tier=tier,
        assignee_id=assignee_id,
        customer_id=customer_id,
        created_after=created_after,
        created_before=created_before,
    )

    stream = service.stream_tickets_csv(principal, filters)
    # The generator is primed here so the row-cap check — which runs before its
    # first yield — raises inside the request handler, where the error mapper
    # can still turn it into a 422. Started lazily inside StreamingResponse it
    # would raise mid-body, after the 200 status line had gone out.
    header = await anext(stream)

    async def body() -> AsyncIterator[str]:
        yield header
        async for chunk in stream:
            yield chunk

    stamp = now().strftime("%Y%m%d")
    return StreamingResponse(
        body(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="tickets-{stamp}.csv"'},
    )
