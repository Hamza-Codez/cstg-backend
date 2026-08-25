"""Notifications (docs/API.md §14).

No deny rows in the matrix: the endpoints take no principal parameter, so there
is no cross-principal access to refuse. Scope differs per role and is decided by
the shared predicate (spec08 §7).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_principal, get_db
from app.core.authorization import Principal
from app.core.unit_of_work import SqlAlchemyUnitOfWork
from app.schemas.notification import (
    MarkReadRequest,
    NotificationCount,
    NotificationPage,
)
from app.services.notification_service import NotificationService

router = APIRouter(
    prefix="/notifications",
    tags=["Notifications"],
    dependencies=[Depends(get_current_principal)],
)


async def get_notification_service(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> NotificationService:
    return NotificationService(SqlAlchemyUnitOfWork(session))


@router.get("", response_model=NotificationPage)
async def list_notifications(
    principal: Annotated[Principal, Depends(get_current_principal)],
    service: Annotated[NotificationService, Depends(get_notification_service)],
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> NotificationPage:
    async with service.uow:
        return await service.page(principal, limit=limit)


@router.get("/count", response_model=NotificationCount)
async def notification_count(
    principal: Annotated[Principal, Depends(get_current_principal)],
    service: Annotated[NotificationService, Depends(get_notification_service)],
) -> NotificationCount:
    """The badge-only path.

    Separate from the list endpoint precisely so the frequent poll stays cheap;
    the UI must not poll the list to derive a count.
    """
    async with service.uow:
        return NotificationCount(unread_count=await service.count(principal))


@router.post("/read", response_model=NotificationCount)
async def mark_read(
    data: MarkReadRequest,
    principal: Annotated[Principal, Depends(get_current_principal)],
    service: Annotated[NotificationService, Depends(get_notification_service)],
) -> NotificationCount:
    async with service.uow:
        await service.mark_read(principal, data.up_to)
        return NotificationCount(unread_count=await service.count(principal))
