"""Admin staff management and configuration (docs/API.md §10–11, FRONTEND.md §7.4)."""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import Category, CustomerTier
from tests.api.test_tickets import (
    create_admin,
    create_agent,
    create_customer,
    create_dispatcher,
    get_auth_token,
    seed_priority_rules,
)

FULL_MATRIX = [
    {"tier": tier, "category": category, "priority": "LOW"}
    for tier in CustomerTier
    for category in Category
]


async def _admin_token(client: AsyncClient, db_session: AsyncSession, email: str) -> str:
    admin = await create_admin(db_session, email)
    return await get_auth_token(client, admin.email)


@pytest.mark.asyncio
@pytest.mark.db
async def test_admin_creates_staff_who_can_sign_in(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _admin_token(client, db_session, "ad_1@example.com")

    created = await client.post(
        "/api/v1/users",
        json={
            "email": "new.agent@example.com",
            "password": "a-good-password",
            "name": "New Agent",
            "role": "AGENT",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert created.status_code == 201
    assert created.json()["is_active"] is True

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "new.agent@example.com", "password": "a-good-password"},
    )
    assert login.status_code == 200
    assert login.json()["role"] == "AGENT"


@pytest.mark.asyncio
@pytest.mark.db
async def test_staff_cannot_be_created_with_the_customer_role(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """`app_user` carries CHECK (role <> 'CUSTOMER') — rejected before the DB sees it."""
    token = await _admin_token(client, db_session, "ad_2@example.com")

    response = await client.post(
        "/api/v1/users",
        json={
            "email": "bad@example.com",
            "password": "a-good-password",
            "name": "Nope",
            "role": "CUSTOMER",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
@pytest.mark.db
async def test_deactivated_agent_can_no_longer_be_assigned(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """INV-8 end to end: deactivation removes them from assignment, not from history."""
    customer = await create_customer(db_session, "ad_c3@example.com")
    agent = await create_agent(db_session, "ad_ag3@example.com")
    admin = await create_admin(db_session, "ad_3@example.com")
    await seed_priority_rules(db_session)
    token_c = await get_auth_token(client, customer.email)
    token_a = await get_auth_token(client, admin.email)

    ticket_id = (
        await client.post(
            "/api/v1/tickets",
            json={"subject": "S", "body": "B", "category": "GENERAL"},
            headers={"Authorization": f"Bearer {token_c}"},
        )
    ).json()["id"]

    deactivated = await client.patch(
        f"/api/v1/users/{agent.id}",
        json={"is_active": False},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert deactivated.status_code == 200
    assert deactivated.json()["is_active"] is False

    rejected = await client.post(
        f"/api/v1/tickets/{ticket_id}/assignment",
        json={"assignee_id": str(agent.id)},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert rejected.status_code == 422

    # And they disappear from the assign picker.
    picker = await client.get(
        "/api/v1/users?role=AGENT&is_active=true",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert str(agent.id) not in [u["id"] for u in picker.json()["items"]]


@pytest.mark.asyncio
@pytest.mark.db
async def test_only_admin_manages_staff(client: AsyncClient, db_session: AsyncSession) -> None:
    dispatcher = await create_dispatcher(db_session, "ad_d4@example.com")
    token = await get_auth_token(client, dispatcher.email)

    response = await client.post(
        "/api/v1/users",
        json={
            "email": "x@example.com",
            "password": "a-good-password",
            "name": "X",
            "role": "AGENT",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.db
async def test_configuration_returns_matrix_and_sla_reference(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await seed_priority_rules(db_session)
    token = await _admin_token(client, db_session, "ad_5@example.com")

    response = await client.get(
        "/api/v1/configuration", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    body = response.json()

    assert len(body["priority_rules"]) == 12, "3 tiers x 4 categories must all be present"
    durations = {d["priority"]: d["seconds"] for d in body["sla_durations"]}
    assert durations == {
        "CRITICAL": 2 * 3600,
        "HIGH": 8 * 3600,
        "MEDIUM": 24 * 3600,
        "LOW": 72 * 3600,
    }


@pytest.mark.asyncio
@pytest.mark.db
async def test_incomplete_matrix_is_rejected(client: AsyncClient, db_session: AsyncSession) -> None:
    """SLA_ENGINE.md §2: the mapping must be total — a gap is a config error, not a default."""
    await seed_priority_rules(db_session)
    token = await _admin_token(client, db_session, "ad_6@example.com")

    response = await client.put(
        "/api/v1/configuration/priority-rules",
        json={"rules": FULL_MATRIX[:-1]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422
    assert "missing" in response.json()["error"]["message"].lower()


@pytest.mark.asyncio
@pytest.mark.db
async def test_full_matrix_replaces_and_takes_effect(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    customer = await create_customer(db_session, "ad_c7@example.com")
    await seed_priority_rules(db_session)
    token_c = await get_auth_token(client, customer.email)
    token = await _admin_token(client, db_session, "ad_7@example.com")

    matrix = [{**row, "priority": "CRITICAL"} for row in FULL_MATRIX]
    updated = await client.put(
        "/api/v1/configuration/priority-rules",
        json={"rules": matrix},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert updated.status_code == 200

    # A new ticket now resolves through the changed matrix.
    created = await client.post(
        "/api/v1/tickets",
        json={"subject": "After", "body": "B", "category": "GENERAL"},
        headers={"Authorization": f"Bearer {token_c}"},
    )
    assert created.json()["priority"] == "CRITICAL"


@pytest.mark.asyncio
@pytest.mark.db
async def test_configuration_is_admin_only(client: AsyncClient, db_session: AsyncSession) -> None:
    dispatcher = await create_dispatcher(db_session, "ad_d8@example.com")
    token = await get_auth_token(client, dispatcher.email)

    assert (
        await client.get("/api/v1/configuration", headers={"Authorization": f"Bearer {token}"})
    ).status_code == 403
