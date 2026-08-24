"""Priority matrix and SLA reference (docs/API.md §11, FRONTEND.md §7.4.2).

Admin-only: the matrix decides every ticket's priority, and priority decides the
SLA window, so this is the most consequential configuration in the system.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_principal, get_db
from app.core.authorization import require_roles
from app.core.unit_of_work import SqlAlchemyUnitOfWork
from app.models.enums import Role
from app.schemas.configuration import ConfigurationResponse, PriorityMatrixUpdate
from app.services.configuration_service import ConfigurationService

router = APIRouter(
    prefix="/configuration",
    tags=["Configuration"],
    dependencies=[Depends(get_current_principal), Depends(require_roles(Role.ADMIN))],
)


async def get_configuration_service(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> ConfigurationService:
    return ConfigurationService(SqlAlchemyUnitOfWork(session))


@router.get("", response_model=ConfigurationResponse)
async def read_configuration(
    service: Annotated[ConfigurationService, Depends(get_configuration_service)],
) -> ConfigurationResponse:
    """The priority matrix, plus SLA durations as a read-only reference (v1)."""
    async with service.uow:
        return await service.get_configuration()


@router.put("/priority-rules", response_model=ConfigurationResponse)
async def replace_priority_rules(
    data: PriorityMatrixUpdate,
    service: Annotated[ConfigurationService, Depends(get_configuration_service)],
) -> ConfigurationResponse:
    """Replace the whole matrix. Rejected with 422 unless it covers every pair."""
    async with service.uow:
        return await service.replace_priority_matrix(data)
