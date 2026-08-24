"""Saved views (docs/API.md §13, spec04 §6).

No update endpoint: renaming or re-filtering is delete + create, and the object
is small enough that a PATCH earns nothing.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_principal, get_db
from app.core.authorization import Principal, require_roles
from app.core.unit_of_work import SqlAlchemyUnitOfWork
from app.models.enums import Role
from app.schemas.saved_view import SavedViewCreate, SavedViewResponse
from app.services.saved_view_service import SavedViewService

router = APIRouter(
    prefix="/saved-views",
    tags=["Saved views"],
    dependencies=[
        Depends(get_current_principal),
        Depends(require_roles(Role.AGENT, Role.DISPATCHER, Role.ADMIN)),
    ],
)


async def get_saved_view_service(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> SavedViewService:
    return SavedViewService(SqlAlchemyUnitOfWork(session))


@router.get("", response_model=dict[str, list[SavedViewResponse]])
async def list_saved_views(
    principal: Annotated[Principal, Depends(get_current_principal)],
    service: Annotated[SavedViewService, Depends(get_saved_view_service)],
) -> dict[str, list[SavedViewResponse]]:
    async with service.uow:
        views = await service.list_views(principal)
    return {"items": [SavedViewResponse.model_validate(v, from_attributes=True) for v in views]}


@router.post("", response_model=SavedViewResponse, status_code=status.HTTP_201_CREATED)
async def create_saved_view(
    data: SavedViewCreate,
    principal: Annotated[Principal, Depends(get_current_principal)],
    service: Annotated[SavedViewService, Depends(get_saved_view_service)],
) -> SavedViewResponse:
    async with service.uow:
        view = await service.create_view(principal, data)
    return SavedViewResponse.model_validate(view, from_attributes=True)


@router.delete("/{view_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_saved_view(
    view_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    service: Annotated[SavedViewService, Depends(get_saved_view_service)],
) -> None:
    async with service.uow:
        await service.delete_view(principal, view_id)
