import pytest
from fastapi import APIRouter, Depends
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_principal
from app.core import security
from app.core.authorization import Principal, require_roles
from app.models.customer import Customer
from app.models.enums import CustomerTier, Role
from app.models.user import AppUser

# A dummy router for testing require_roles
test_router = APIRouter()


@test_router.get("/protected-admin", dependencies=[Depends(get_current_principal)])
async def protected_admin(
    principal: Principal = Depends(require_roles(Role.ADMIN)),
) -> dict[str, str]:
    return {"status": "ok"}


@pytest.mark.asyncio
@pytest.mark.db
async def test_login_success_customer(client: AsyncClient, db_session: AsyncSession) -> None:
    hashed_pw = security.get_password_hash("password123")
    customer = Customer(
        email="cust@example.com", password_hash=hashed_pw, name="Test Cust", tier=CustomerTier.FREE
    )
    db_session.add(customer)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/auth/login", json={"email": "cust@example.com", "password": "password123"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["token_type"] == "bearer"
    assert "access_token" in data
    assert data["role"] == "CUSTOMER"
    assert data["principal_type"] == "CUSTOMER"


@pytest.mark.asyncio
@pytest.mark.db
async def test_login_success_user(client: AsyncClient, db_session: AsyncSession) -> None:
    hashed_pw = security.get_password_hash("password123")
    user = AppUser(
        email="agent@example.com",
        password_hash=hashed_pw,
        name="Test Agent",
        role=Role.AGENT,
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/auth/login", json={"email": "agent@example.com", "password": "password123"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["role"] == "AGENT"
    assert data["principal_type"] == "USER"


@pytest.mark.asyncio
@pytest.mark.db
async def test_login_failure_invalid_creds(client: AsyncClient, db_session: AsyncSession) -> None:
    resp = await client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.com", "password": "wrong"}
    )
    assert resp.status_code == 401
    data = resp.json()
    assert data["error"]["code"] == "UNAUTHENTICATED"


@pytest.mark.asyncio
@pytest.mark.db
async def test_login_inactive_user(client: AsyncClient, db_session: AsyncSession) -> None:
    hashed_pw = security.get_password_hash("password123")
    user = AppUser(
        email="inactive@example.com",
        password_hash=hashed_pw,
        name="Inactive",
        role=Role.AGENT,
        is_active=False,
    )
    db_session.add(user)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/auth/login", json={"email": "inactive@example.com", "password": "password123"}
    )
    assert resp.status_code == 401
    data = resp.json()
    assert data["error"]["code"] == "UNAUTHENTICATED"


@pytest.mark.asyncio
@pytest.mark.db
async def test_require_roles_enforcement(client: AsyncClient, db_session: AsyncSession) -> None:
    # Register dummy router for testing on the fly
    client._transport.app.include_router(test_router)  # type: ignore

    # 1. Login as an agent
    hashed_pw = security.get_password_hash("password123")
    user = AppUser(
        email="agent2@example.com",
        password_hash=hashed_pw,
        name="Agent 2",
        role=Role.AGENT,
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/auth/login", json={"email": "agent2@example.com", "password": "password123"}
    )
    token = resp.json()["access_token"]

    # 2. Access protected endpoint (Admin only) - should fail
    resp2 = await client.get("/protected-admin", headers={"Authorization": f"Bearer {token}"})
    assert resp2.status_code == 403
    assert resp2.json()["error"]["code"] == "FORBIDDEN"
