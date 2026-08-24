"""Staff directory (docs/API.md §10) — feeds the dispatcher's assign picker."""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.api.test_tickets import (
    create_admin,
    create_agent,
    create_customer,
    create_dispatcher,
    get_auth_token,
)


@pytest.mark.asyncio
@pytest.mark.db
async def test_dispatcher_can_list_active_agents(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_agent(db_session, "u_active@example.com")
    await create_agent(db_session, "u_inactive@example.com", is_active=False)
    dispatcher = await create_dispatcher(db_session, "u_disp@example.com")
    token = await get_auth_token(client, dispatcher.email)

    response = await client.get(
        "/api/v1/users?role=AGENT&is_active=true",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    emails = [u["email"] for u in response.json()["items"]]

    assert "u_active@example.com" in emails
    # INV-8: an inactive agent must never be offered as an assignee.
    assert "u_inactive@example.com" not in emails


@pytest.mark.asyncio
@pytest.mark.db
async def test_directory_never_exposes_password_material(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_agent(db_session, "u_a2@example.com")
    admin = await create_admin(db_session, "u_adm2@example.com")
    token = await get_auth_token(client, admin.email)

    response = await client.get("/api/v1/users", headers={"Authorization": f"Bearer {token}"})
    body = response.text
    assert "password" not in body.lower()


@pytest.mark.asyncio
@pytest.mark.db
async def test_agents_and_customers_cannot_read_the_directory(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Agents cannot reassign, so they have no reason to enumerate staff."""
    agent = await create_agent(db_session, "u_a3@example.com")
    customer = await create_customer(db_session, "u_c3@example.com")

    for email in (agent.email, customer.email):
        token = await get_auth_token(client, email)
        response = await client.get("/api/v1/users", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 403
