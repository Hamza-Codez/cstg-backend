"""FastAPI dependencies.

P0 provides the database session only. ``get_current_principal`` and the service
providers arrive in P2/P3.
"""

from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.authorization import Principal
from app.core.security import decode_access_token
from app.core.storage import StorageBackend, build_storage
from app.domain.errors import Unauthenticated
from app.models.enums import ActorType, Role
from app.repositories.customer_repo import CustomerRepository
from app.repositories.user_repo import UserRepository
from app.services.auth_service import AuthService


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield a session for the request."""
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with session_factory() as session:
        yield session


def get_auth_service(session: AsyncSession = Depends(get_db)) -> AuthService:
    return AuthService(session)


async def get_current_principal(
    request: Request, session: AsyncSession = Depends(get_db)
) -> Principal:
    """
    Extract JWT, decode, and hit DB to verify user is active.
    Raises Forbidden (which maps to 401 via error handlers) if invalid.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise Unauthenticated("Not authenticated.")

    token = auth_header.split(" ")[1]

    try:
        payload = decode_access_token(token)
    except Exception:
        # `from None`: the decode failure must not leak into the response or logs
        # as a cause chain — every auth failure looks identical to the caller.
        raise Unauthenticated("Not authenticated.") from None

    sub = payload.get("sub")
    p_type = payload.get("principal_type")

    if not sub or not p_type:
        raise Unauthenticated("Not authenticated.")

    try:
        principal_id = UUID(sub)
    except ValueError:
        raise Unauthenticated("Not authenticated.") from None

    # Lookups go through repositories: the api/ layer must not touch the ORM
    # directly (BACKEND_STRUCTURE.md §3).
    if p_type == ActorType.USER.value:
        user = await UserRepository(session).get(principal_id)
        if not user or not user.is_active:
            raise Unauthenticated("Not authenticated.")
        principal = Principal(
            id=user.id, type=ActorType.USER, role=user.role, is_active=user.is_active
        )
    elif p_type == ActorType.CUSTOMER.value:
        customer = await CustomerRepository(session).get(principal_id)
        if not customer:
            raise Unauthenticated("Not authenticated.")
        principal = Principal(
            id=customer.id, type=ActorType.CUSTOMER, role=Role.CUSTOMER, is_active=True
        )
    else:
        raise Unauthenticated("Not authenticated.")

    request.state.principal = principal
    return principal


def get_storage(request: Request) -> StorageBackend:
    """The configured storage backend (spec03 §3).

    Built once per process and held on app state, so LocalStorage resolves its
    root a single time and MemoryStorage keeps one dict for the app's lifetime
    rather than one per request.
    """
    storage: StorageBackend | None = getattr(request.app.state, "storage", None)
    if storage is None:
        settings = request.app.state.settings
        storage = build_storage(settings.storage_backend, settings.storage_root)
        request.app.state.storage = storage
    return storage
