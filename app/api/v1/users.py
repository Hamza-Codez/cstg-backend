"""Staff directory and management (docs/API.md §10).

Listing serves the dispatcher's assign picker (UIUX_FRONTEND.md §7.3.2) and is open
to DISPATCHER and ADMIN. Creating and activating/deactivating staff is admin-only
(AUTHORIZATION.md §3).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_principal, get_db
from app.core.authorization import require_roles
from app.core.unit_of_work import SqlAlchemyUnitOfWork
from app.models.enums import Role
from app.repositories.user_repo import UserRepository
from app.schemas.user import UserCreate, UserSummary, UserUpdate
from app.services.user_service import UserService

router = APIRouter(
    prefix="/users",
    tags=["Users"],
    dependencies=[Depends(get_current_principal)],
)


@router.get(
    "",
    response_model=dict[str, list[UserSummary]],
    # Only the roles that assign work need the staff directory. Agents are
    # deliberately excluded: they cannot reassign (UIUX_FRONTEND.md §7.2.5).
    dependencies=[Depends(require_roles(Role.DISPATCHER, Role.ADMIN))],
)
async def list_users(
    session: Annotated[AsyncSession, Depends(get_db)],
    role: Annotated[Role | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
) -> dict[str, list[UserSummary]]:
    users = await UserRepository(session).list_staff(role=role, is_active=is_active)
    return {"items": [UserSummary.model_validate(u, from_attributes=True) for u in users]}


async def get_user_service(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> UserService:
    return UserService(SqlAlchemyUnitOfWork(session))


@router.post(
    "",
    response_model=UserSummary,
    status_code=status.HTTP_201_CREATED,
    # Staff management is admin-only (AUTHORIZATION.md §3).
    dependencies=[Depends(require_roles(Role.ADMIN))],
)
async def create_user(
    data: UserCreate,
    service: Annotated[UserService, Depends(get_user_service)],
) -> UserSummary:
    """Create an agent, dispatcher, or admin."""
    async with service.uow:
        user = await service.create(data)
    return UserSummary.model_validate(user, from_attributes=True)


@router.patch(
    "/{user_id}",
    response_model=UserSummary,
    dependencies=[Depends(require_roles(Role.ADMIN))],
)
async def update_user(
    user_id: uuid.UUID,
    data: UserUpdate,
    service: Annotated[UserService, Depends(get_user_service)],
) -> UserSummary:
    """Activate or deactivate. A deactivated agent can no longer be assigned (INV-8)."""
    async with service.uow:
        user = await service.set_active(user_id, data)
    return UserSummary.model_validate(user, from_attributes=True)
