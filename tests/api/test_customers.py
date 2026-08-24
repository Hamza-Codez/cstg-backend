"""Customer registration (docs/API.md §3)."""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.api.test_tickets import create_customer, seed_priority_rules


@pytest.mark.asyncio
@pytest.mark.db
async def test_register_creates_a_free_tier_customer(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    response = await client.post(
        "/api/v1/customers",
        json={"email": "new@example.com", "password": "a-good-password", "name": "New Person"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "new@example.com"
    assert body["tier"] == "FREE", "sign-up must never grant a faster SLA tier"
    assert "password" not in body and "password_hash" not in body


@pytest.mark.asyncio
@pytest.mark.db
async def test_registered_customer_can_sign_in_and_create_a_ticket(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await seed_priority_rules(db_session)
    await client.post(
        "/api/v1/customers",
        json={"email": "flow@example.com", "password": "a-good-password", "name": "Flow"},
    )

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "flow@example.com", "password": "a-good-password"},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]

    created = await client.post(
        "/api/v1/tickets",
        json={"subject": "First", "body": "Body", "category": "GENERAL"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert created.status_code == 201


@pytest.mark.asyncio
@pytest.mark.db
async def test_duplicate_email_does_not_reveal_the_account(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_customer(db_session, "taken@example.com")

    response = await client.post(
        "/api/v1/customers",
        json={"email": "taken@example.com", "password": "a-good-password", "name": "Impostor"},
    )
    assert response.status_code == 422
    assert "already" not in response.json()["error"]["message"].lower()


@pytest.mark.asyncio
@pytest.mark.db
async def test_email_uniqueness_is_case_insensitive(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """`email` is citext (DATABASE.md §3), so casing must not create a second account."""
    await create_customer(db_session, "Mixed@Example.com")

    response = await client.post(
        "/api/v1/customers",
        json={"email": "mixed@example.com", "password": "a-good-password", "name": "Dup"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.db
async def test_short_password_is_rejected(client: AsyncClient, db_session: AsyncSession) -> None:
    response = await client.post(
        "/api/v1/customers",
        json={"email": "weak@example.com", "password": "short", "name": "Weak"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
