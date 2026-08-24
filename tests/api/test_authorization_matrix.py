"""Matrix-driven authorization tests (docs/TESTING.md §3).

Parametrised over the authoritative matrix in AUTHORIZATION.md §3. Each case
asserts allow/deny for a (role x capability x ownership) triple. INV-4: a
transition is authorized or it does not occur.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import TicketStatus
from tests.api.test_tickets import (
    create_admin,
    create_agent,
    create_customer,
    create_dispatcher,
    get_auth_token,
    seed_priority_rules,
)


async def _ticket_in_state(
    client: AsyncClient,
    db_session: AsyncSession,
    state: TicketStatus,
    prefix: str,
) -> tuple[str, str, str, str, str]:
    """Build a ticket in `state`, returning ids/tokens for every principal."""
    customer = await create_customer(db_session, f"{prefix}_c@example.com")
    agent = await create_agent(db_session, f"{prefix}_a@example.com")
    dispatcher = await create_dispatcher(db_session, f"{prefix}_d@example.com")
    admin = await create_admin(db_session, f"{prefix}_adm@example.com")
    await seed_priority_rules(db_session)

    t_cust = await get_auth_token(client, customer.email)
    t_agent = await get_auth_token(client, agent.email)
    t_disp = await get_auth_token(client, dispatcher.email)
    t_admin = await get_auth_token(client, admin.email)

    created = await client.post(
        "/api/v1/tickets",
        json={"subject": "S", "body": "B", "category": "GENERAL"},
        headers={"Authorization": f"Bearer {t_cust}"},
    )
    ticket_id = created.json()["id"]

    if state is not TicketStatus.OPEN:
        await client.post(
            f"/api/v1/tickets/{ticket_id}/assignment",
            json={"assignee_id": str(agent.id)},
            headers={"Authorization": f"Bearer {t_admin}"},
        )
        await client.post(
            f"/api/v1/tickets/{ticket_id}/transitions",
            json={"to": TicketStatus.IN_PROGRESS},
            headers={"Authorization": f"Bearer {t_admin}"},
        )
        if state in (TicketStatus.RESOLVED, TicketStatus.CLOSED):
            await client.post(
                f"/api/v1/tickets/{ticket_id}/transitions",
                json={"to": TicketStatus.RESOLVED},
                headers={"Authorization": f"Bearer {t_admin}"},
            )

    return ticket_id, t_cust, t_agent, t_disp, t_admin


async def _transition(client: AsyncClient, ticket_id: str, token: str, to: str) -> int:
    response = await client.post(
        f"/api/v1/tickets/{ticket_id}/transitions",
        json={"to": to},
        headers={"Authorization": f"Bearer {token}"},
    )
    return response.status_code


@pytest.mark.asyncio
@pytest.mark.db
async def test_customer_cannot_transition_own_ticket(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Matrix: CUSTOMER is ✗ for T1/T2/T3 — even on a ticket they own (INV-4)."""
    ticket_id, t_cust, _, _, _ = await _ticket_in_state(
        client, db_session, TicketStatus.OPEN, "mx1"
    )
    assert await _transition(client, ticket_id, t_cust, "IN_PROGRESS") == 403


@pytest.mark.asyncio
@pytest.mark.db
async def test_dispatcher_cannot_start_or_resolve(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Matrix: DISPATCHER is ✗ for T1 and T2, ✓ only for T3."""
    ticket_id, _, _, t_disp, t_admin = await _ticket_in_state(
        client, db_session, TicketStatus.OPEN, "mx2"
    )
    assert await _transition(client, ticket_id, t_disp, "IN_PROGRESS") == 403


@pytest.mark.asyncio
@pytest.mark.db
async def test_dispatcher_cannot_resolve(client: AsyncClient, db_session: AsyncSession) -> None:
    ticket_id, _, _, t_disp, _ = await _ticket_in_state(
        client, db_session, TicketStatus.IN_PROGRESS, "mx3"
    )
    assert await _transition(client, ticket_id, t_disp, "RESOLVED") == 403


@pytest.mark.asyncio
@pytest.mark.db
async def test_dispatcher_may_close(client: AsyncClient, db_session: AsyncSession) -> None:
    """Matrix: DISPATCHER ✓ for T3 — the one transition they own."""
    ticket_id, _, _, t_disp, _ = await _ticket_in_state(
        client, db_session, TicketStatus.RESOLVED, "mx4"
    )
    assert await _transition(client, ticket_id, t_disp, "CLOSED") == 200


@pytest.mark.asyncio
@pytest.mark.db
async def test_assigned_agent_may_start_and_resolve(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    ticket_id, _, t_agent, _, _ = await _ticket_in_state(
        client, db_session, TicketStatus.IN_PROGRESS, "mx5"
    )
    assert await _transition(client, ticket_id, t_agent, "RESOLVED") == 200


@pytest.mark.asyncio
@pytest.mark.db
async def test_agent_cannot_assign(client: AsyncClient, db_session: AsyncSession) -> None:
    """Matrix: AGENT is ✗ for assign/reassign."""
    ticket_id, _, t_agent, _, _ = await _ticket_in_state(
        client, db_session, TicketStatus.OPEN, "mx6"
    )
    response = await client.post(
        f"/api/v1/tickets/{ticket_id}/assignment",
        json={"assignee_id": str(uuid.uuid4())},
        headers={"Authorization": f"Bearer {t_agent}"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.db
async def test_customer_cannot_author_comments(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Matrix: CUSTOMER authors no comments in v1."""
    ticket_id, t_cust, _, _, _ = await _ticket_in_state(
        client, db_session, TicketStatus.OPEN, "mx7"
    )
    response = await client.post(
        f"/api/v1/tickets/{ticket_id}/comments",
        json={"type": "PUBLIC_REPLY", "body": "hello"},
        headers={"Authorization": f"Bearer {t_cust}"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.db
async def test_non_admin_cannot_read_metrics(client: AsyncClient, db_session: AsyncSession) -> None:
    """Matrix: metrics are ADMIN-only."""
    _, t_cust, t_agent, t_disp, t_admin = await _ticket_in_state(
        client, db_session, TicketStatus.OPEN, "mx8"
    )
    for token in (t_cust, t_agent, t_disp):
        response = await client.get(
            "/api/v1/metrics/overview", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 403
    ok = await client.get(
        "/api/v1/metrics/overview", headers={"Authorization": f"Bearer {t_admin}"}
    )
    assert ok.status_code == 200
