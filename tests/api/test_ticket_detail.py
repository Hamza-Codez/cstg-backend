"""Ticket detail contract and timeline scoping (docs/API.md §4, INV-9)."""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import TicketStatus
from tests.api.test_tickets import (
    create_admin,
    create_agent,
    create_customer,
    get_auth_token,
    seed_priority_rules,
)


@pytest.mark.asyncio
@pytest.mark.db
async def test_detail_returns_body_assignee_and_timeline(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    customer = await create_customer(db_session, "d_cust@example.com")
    admin = await create_admin(db_session, "d_admin@example.com")
    agent = await create_agent(db_session, "d_agent@example.com")
    await seed_priority_rules(db_session)
    token_c = await get_auth_token(client, customer.email)
    token_a = await get_auth_token(client, admin.email)

    created = await client.post(
        "/api/v1/tickets",
        json={"subject": "Printer down", "body": "It will not print", "category": "GENERAL"},
        headers={"Authorization": f"Bearer {token_c}"},
    )
    ticket_id = created.json()["id"]

    await client.post(
        f"/api/v1/tickets/{ticket_id}/assignment",
        json={"assignee_id": str(agent.id)},
        headers={"Authorization": f"Bearer {token_a}"},
    )

    detail = await client.get(
        f"/api/v1/tickets/{ticket_id}", headers={"Authorization": f"Bearer {token_a}"}
    )
    assert detail.status_code == 200
    data = detail.json()

    # API.md §4: TicketResponse + body + assignee summary + timeline.
    assert data["body"] == "It will not print"
    assert data["assignee"]["name"] == agent.name
    assert data["assignee"]["id"] == str(agent.id)

    types = [event["type"] for event in data["timeline"]]
    assert "CREATED" in types
    assert "ASSIGNMENT" in types, "staff see the full activity trail"


@pytest.mark.asyncio
@pytest.mark.db
async def test_customer_timeline_hides_internal_routing(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A customer must not learn who works their ticket, or that notes exist."""
    customer = await create_customer(db_session, "d_cust2@example.com")
    admin = await create_admin(db_session, "d_admin2@example.com")
    agent = await create_agent(db_session, "d_agent2@example.com")
    await seed_priority_rules(db_session)
    token_c = await get_auth_token(client, customer.email)
    token_a = await get_auth_token(client, admin.email)

    created = await client.post(
        "/api/v1/tickets",
        json={"subject": "Slow site", "body": "Very slow", "category": "TECHNICAL"},
        headers={"Authorization": f"Bearer {token_c}"},
    )
    ticket_id = created.json()["id"]

    await client.post(
        f"/api/v1/tickets/{ticket_id}/assignment",
        json={"assignee_id": str(agent.id)},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    await client.post(
        f"/api/v1/tickets/{ticket_id}/comments",
        json={"type": "INTERNAL_NOTE", "body": "Customer is on the free plan"},
        headers={"Authorization": f"Bearer {token_a}"},
    )

    detail = await client.get(
        f"/api/v1/tickets/{ticket_id}", headers={"Authorization": f"Bearer {token_c}"}
    )
    assert detail.status_code == 200
    types = [event["type"] for event in detail.json()["timeline"]]

    assert "CREATED" in types
    assert "ASSIGNMENT" not in types
    assert "COMMENT" not in types


@pytest.mark.asyncio
@pytest.mark.db
async def test_detail_hides_other_customers_tickets(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    owner = await create_customer(db_session, "d_owner@example.com")
    other = await create_customer(db_session, "d_other@example.com")
    await seed_priority_rules(db_session)
    token_owner = await get_auth_token(client, owner.email)
    token_other = await get_auth_token(client, other.email)

    created = await client.post(
        "/api/v1/tickets",
        json={"subject": "Private", "body": "Private body", "category": "BILLING"},
        headers={"Authorization": f"Bearer {token_owner}"},
    )
    ticket_id = created.json()["id"]

    hidden = await client.get(
        f"/api/v1/tickets/{ticket_id}", headers={"Authorization": f"Bearer {token_other}"}
    )
    assert hidden.status_code == 404, "hidden existence is a 404, never a 403"


@pytest.mark.asyncio
@pytest.mark.db
async def test_status_change_appears_in_customer_timeline(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    customer = await create_customer(db_session, "d_cust3@example.com")
    admin = await create_admin(db_session, "d_admin3@example.com")
    agent = await create_agent(db_session, "d_agent3@example.com")
    await seed_priority_rules(db_session)
    token_c = await get_auth_token(client, customer.email)
    token_a = await get_auth_token(client, admin.email)

    created = await client.post(
        "/api/v1/tickets",
        json={"subject": "Login issue", "body": "Cannot log in", "category": "TECHNICAL"},
        headers={"Authorization": f"Bearer {token_c}"},
    )
    ticket_id = created.json()["id"]

    await client.post(
        f"/api/v1/tickets/{ticket_id}/assignment",
        json={"assignee_id": str(agent.id)},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    await client.post(
        f"/api/v1/tickets/{ticket_id}/transitions",
        json={"to": TicketStatus.IN_PROGRESS},
        headers={"Authorization": f"Bearer {token_a}"},
    )

    detail = await client.get(
        f"/api/v1/tickets/{ticket_id}", headers={"Authorization": f"Bearer {token_c}"}
    )
    timeline = detail.json()["timeline"]
    changes = [e for e in timeline if e["type"] == "STATUS_CHANGE"]
    assert len(changes) == 1
    assert changes[0]["to_status"] == "IN_PROGRESS"
