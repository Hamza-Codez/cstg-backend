"""Notifications (docs/API.md §14).

No deny rows in the matrix: the endpoints take no principal parameter, so there
is no cross-principal access to refuse. Scope differs per role and is decided by
the shared predicate (spec08 §7).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_principal, get_db
from app.core.authorization import Principal
from app.core.pagination import InvalidCursor, decode_cursor
from app.core.unit_of_work import SqlAlchemyUnitOfWork
from app.domain.errors import ValidationError
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
    cursor: str | None = None,
) -> NotificationPage:
    """Recent notifications, **read and unread**, newest first.

    Keyset-paginated on `(created_at, id)` like every other list in this API —
    no OFFSET, so a page neither skips nor repeats when new events arrive
    mid-scroll, which on an append-only log they constantly do.
    """
    decoded = None
    if cursor:
        try:
            decoded = decode_cursor(cursor)
        except InvalidCursor as exc:
            raise ValidationError(str(exc)) from exc

    async with service.uow:
        return await service.page(principal, limit=limit, cursor=decoded)


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


@router.delete("/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
async def dismiss_notification(
    event_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    service: Annotated[NotificationService, Depends(get_notification_service)],
) -> Response:
    """Hide one notification from the caller's own feed.

    The `ticket_event` behind it is **not** deleted — it cannot be, and should
    not be. It is the audit log, and one person tidying their inbox must not
    erase a record the whole system depends on. Only this principal's view
    changes; everyone else still sees it.

    An event the caller may not see returns **404**, identical to one that does
    not exist. Anything that distinguished the two would let a caller enumerate
    event ids (INV-9). Repeat calls are 204.
    """
    async with service.uow:
        await service.dismiss(principal, event_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/clear", response_model=NotificationCount)
async def clear_notifications(
    principal: Annotated[Principal, Depends(get_current_principal)],
    service: Annotated[NotificationService, Depends(get_notification_service)],
) -> NotificationCount:
    """Empty the caller's feed up to now.

    Stored as a timestamp rather than a dismissal row per notification: the
    latter is an unbounded write inside a request handler, in the process that
    also hosts the SLA monitor. Events created afterwards arrive normally.
    """
    async with service.uow:
        await service.clear_all(principal)
        return NotificationCount(unread_count=await service.count(principal))
