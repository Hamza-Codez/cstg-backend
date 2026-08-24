from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_principal, get_db
from app.core.authorization import Principal, require_roles
from app.models.enums import Role
from app.repositories.metrics_repo import MetricsRepository
from app.schemas.metrics import MetricsOverview
from app.services.metrics_service import MetricsService

router = APIRouter(
    prefix="/metrics", tags=["Metrics"], dependencies=[Depends(get_current_principal)]
)


async def get_metrics_service(session: Annotated[AsyncSession, Depends(get_db)]) -> MetricsService:
    return MetricsService(MetricsRepository(session))


@router.get(
    "/overview",
    response_model=MetricsOverview,
    dependencies=[Depends(require_roles(Role.ADMIN))],
)
async def get_overview(
    principal: Annotated[Principal, Depends(get_current_principal)],
    service: Annotated[MetricsService, Depends(get_metrics_service)],
) -> MetricsOverview:
    """
    Get global and priority-scoped metrics (Admin only).
    """
    return await service.get_overview_metrics()
