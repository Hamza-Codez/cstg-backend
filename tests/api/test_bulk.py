import uuid

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core import security
from app.models.customer import Customer
from app.models.enums import CustomerTier, Role
from app.models.user import AppUser


async def create_customer(db_session: AsyncSession, email: str = "cust@example.com") -> Customer:
    customer = Customer(
        email=email,
        password_hash=security.get_password_hash("password"),
        name="Test Cust",
        tier=CustomerTier.FREE,
    )
    db_session.add(customer)
    await db_session.commit()
    await db_session.refresh(customer)
    return customer


async def create_dispatcher(db_session: AsyncSession, email: str = "disp@example.com") -> AppUser:
    user = AppUser(
        email=email,
        password_hash=security.get_password_hash("password"),
        name="Test Dispatcher",
        role=Role.DISPATCHER,
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def get_auth_token(client: AsyncClient, email: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": "password"})
    return str(resp.json()["access_token"])


@pytest.mark.asyncio
@pytest.mark.db
async def test_bulk_max_items(client: AsyncClient, db_session: AsyncSession) -> None:
    dispatcher = await create_dispatcher(db_session)
    token = await get_auth_token(client, dispatcher.email)

    settings = get_settings()
    max_items = settings.bulk_max_items
    ticket_ids = [str(uuid.uuid4()) for _ in range(max_items + 1)]

    response = await client.post(
        "/api/v1/tickets/bulk/assignment",
        json={"ticket_ids": ticket_ids, "assignee_id": str(uuid.uuid4())},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "cannot exceed" in response.json()["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.db
async def test_bulk_duplicate_ids(client: AsyncClient, db_session: AsyncSession) -> None:
    dispatcher = await create_dispatcher(db_session)
    token = await get_auth_token(client, dispatcher.email)

    ticket_id = str(uuid.uuid4())
    response = await client.post(
        "/api/v1/tickets/bulk/assignment",
        json={"ticket_ids": [ticket_id, ticket_id], "assignee_id": str(uuid.uuid4())},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "Duplicate" in response.json()["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.db
async def test_bulk_empty_array(client: AsyncClient, db_session: AsyncSession) -> None:
    dispatcher = await create_dispatcher(db_session)
    token = await get_auth_token(client, dispatcher.email)

    response = await client.post(
        "/api/v1/tickets/bulk/assignment",
        json={"ticket_ids": [], "assignee_id": str(uuid.uuid4())},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "at least one" in response.json()["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.db
async def test_bulk_transition_invalid_status(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    dispatcher = await create_dispatcher(db_session)
    token = await get_auth_token(client, dispatcher.email)

    ticket_id = str(uuid.uuid4())
    response = await client.post(
        "/api/v1/tickets/bulk/transitions",
        json={"ticket_ids": [ticket_id], "to": "RESOLVED"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.asyncio
@pytest.mark.db
async def test_bulk_customer_forbidden(client: AsyncClient, db_session: AsyncSession) -> None:
    customer = await create_customer(db_session)
    token = await get_auth_token(client, customer.email)

    response = await client.post(
        "/api/v1/tickets/bulk/assignment",
        json={"ticket_ids": [str(uuid.uuid4())], "assignee_id": str(uuid.uuid4())},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN
