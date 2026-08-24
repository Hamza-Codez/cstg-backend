import asyncio

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ticket import Ticket
from tests.api.test_tickets import (
    create_admin,
    create_customer,
    get_auth_token,
    seed_priority_rules,
)


@pytest.mark.asyncio
@pytest.mark.db
async def test_concurrent_ticket_transition(client: AsyncClient, db_session: AsyncSession) -> None:
    customer = await create_customer(db_session, "conc_cust@example.com")
    admin = await create_admin(db_session, "conc_admin@example.com")
    await seed_priority_rules(db_session)

    token_c = await get_auth_token(client, customer.email)
    resp = await client.post(
        "/api/v1/tickets",
        json={"subject": "Race Condition", "body": "Issue", "category": "GENERAL"},
        headers={"Authorization": f"Bearer {token_c}"},
    )
    assert resp.status_code == 201
    ticket_id = resp.json()["id"]

    # We need to manually assign the ticket so it can transition to IN_PROGRESS
    await db_session.execute(
        update(Ticket).where(Ticket.id == ticket_id).values(assignee_id=admin.id)
    )
    await db_session.commit()

    token_a = await get_auth_token(client, admin.email)

    # Two concurrent requests to transition OPEN -> IN_PROGRESS
    async def try_transition() -> httpx.Response:
        return await client.post(
            f"/api/v1/tickets/{ticket_id}/transitions",
            json={"to": "IN_PROGRESS"},
            headers={"Authorization": f"Bearer {token_a}"},
        )

    # We run them concurrently
    results = await asyncio.gather(try_transition(), try_transition())

    status_codes = [r.status_code for r in results]

    # One should succeed (200 OK) and the other should fail (409 Conflict)
    assert 200 in status_codes
    assert 409 in status_codes

    # Make sure we didn't get any 500s or weird behavior
    assert sorted(status_codes) == [200, 409]
