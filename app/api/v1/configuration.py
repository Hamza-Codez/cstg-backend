"""Priority matrix and SLA reference (docs/API.md §11, FRONTEND.md §7.4.2).

Admin-only: the matrix decides every ticket's priority, and priority decides the
SLA window, so this is the most consequential configuration in the system.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_principal, get_db
from app.core.authorization import Principal, require_roles
from app.core.unit_of_work import SqlAlchemyUnitOfWork
from app.models.enums import Role
from app.schemas.configuration import (
    ConfigurationResponse,
    PriorityMatrixUpdate,
    SlaDurationEntry,
    SlaPolicyUpdate,
    SlaPolicyVersionSummary,
)
from app.services.configuration_service import ConfigurationService
from app.services.sla_policy_service import SlaPolicyService

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


@router.put("/sla-policy", response_model=ConfigurationResponse)
async def replace_sla_policy(
    data: SlaPolicyUpdate,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> ConfigurationResponse:
    """Publish a new SLA policy version (docs/API.md §11).

    The whole policy at once, because it must stay total. Affects **new tickets
    only** — priority and deadline are frozen at creation (INV-1), so existing
    tickets keep the terms they were created under.
    """
    uow = SqlAlchemyUnitOfWork(session)
    async with uow:
        await SlaPolicyService(uow).activate(
            principal,
            {entry.priority: entry.seconds for entry in data.durations},
            note=data.note,
        )
        return await ConfigurationService(uow).get_configuration()


@router.get("/sla-policy/history", response_model=dict[str, list[SlaPolicyVersionSummary]])
async def sla_policy_history(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, list[SlaPolicyVersionSummary]]:
    """Every version, newest first.

    This is what makes a frozen deadline explainable: a ticket showing a
    six-hour window when the config says eight is otherwise indistinguishable
    from a bug.
    """
    uow = SqlAlchemyUnitOfWork(session)
    async with uow:
        versions = await uow.sla_policies.list_versions()
        entries = await uow.sla_policies.all_entries()

    by_version: dict[uuid.UUID, list[SlaDurationEntry]] = {}
    for entry in entries:
        by_version.setdefault(entry.version_id, []).append(
            SlaDurationEntry(priority=entry.priority, seconds=entry.seconds)
        )

    return {
        "items": [
            SlaPolicyVersionSummary(
                version_id=v.id,
                activated_at=v.activated_at,
                superseded_at=v.superseded_at,
                created_at=v.created_at,
                note=v.note,
                is_active=v.activated_at is not None and v.superseded_at is None,
                durations=sorted(by_version.get(v.id, []), key=lambda d: d.seconds),
            )
            for v in versions
        ]
    }
