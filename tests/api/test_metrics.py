import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import now
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
async def test_metrics_rbac(client: AsyncClient, db_session: AsyncSession) -> None:
    customer = await create_customer(db_session, "m_cust@example.com")
    agent = await create_agent(db_session, "m_agent@example.com")
    token_c = await get_auth_token(client, customer.email)
    token_a = await get_auth_token(client, agent.email)

    resp_c = await client.get(
        "/api/v1/metrics/overview", headers={"Authorization": f"Bearer {token_c}"}
    )
    assert resp_c.status_code == 403

    resp_a = await client.get(
        "/api/v1/metrics/overview", headers={"Authorization": f"Bearer {token_a}"}
    )
    assert resp_a.status_code == 403


@pytest.mark.asyncio
@pytest.mark.db
async def test_metrics_aggregation(client: AsyncClient, db_session: AsyncSession) -> None:
    customer = await create_customer(db_session, "m_cust2@example.com")
    admin = await create_admin(db_session, "m_admin@example.com")
    agent = await create_agent(db_session, "m_agent2@example.com")
    await seed_priority_rules(db_session)
    token_c = await get_auth_token(client, customer.email)
    token_a = await get_auth_token(client, admin.email)

    # Create ticket 1: OPEN, LOW
    resp1 = await client.post(
        "/api/v1/tickets",
        json={"subject": "T1", "body": "B1", "category": "GENERAL"},
        headers={"Authorization": f"Bearer {token_c}"},
    )
    t1_id = resp1.json()["id"]

    # Create ticket 2: RESOLVED, LOW
    resp2 = await client.post(
        "/api/v1/tickets",
        json={"subject": "T2", "body": "B2", "category": "GENERAL"},
        headers={"Authorization": f"Bearer {token_c}"},
    )
    t2_id = resp2.json()["id"]

    # Assign and resolve T2
    resp_assign = await client.post(
        f"/api/v1/tickets/{t2_id}/assignment",
        json={"assignee_id": str(agent.id)},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp_assign.status_code == 200

    resp_ip = await client.post(
        f"/api/v1/tickets/{t2_id}/transitions",
        json={"to": TicketStatus.IN_PROGRESS},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp_ip.status_code == 200

    resp_trans = await client.post(
        f"/api/v1/tickets/{t2_id}/transitions",
        json={"to": TicketStatus.RESOLVED},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp_trans.status_code == 200

    # Let's forcefully breach T1 for the test via DB
    from datetime import timedelta

    from sqlalchemy import update

    from app.models.ticket import Ticket

    # Both timestamps move: `breached_only_when_past_due` checks sla_due_at, not
    # deadline (P16). With no accrued pause the two are equal, which is INV-13.
    past = now() - timedelta(minutes=5)
    await db_session.execute(
        update(Ticket)
        .where(Ticket.id == t1_id)
        .values(
            deadline=past,
            sla_due_at=past,
            sla_breached_at=now(),
        )
    )
    await db_session.commit()

    # Now get metrics
    resp_m = await client.get(
        "/api/v1/metrics/overview", headers={"Authorization": f"Bearer {token_a}"}
    )
    assert resp_m.status_code == 200
    data = resp_m.json()

    assert data["open"] == 1
    assert data["resolved"] == 1
    assert data["closed"] == 0
    assert data["breached_open"] == 1
    assert data["breach_rate"] == 0.5  # 1 breached / 2 total

    low_metrics = data["by_priority"]["LOW"]
    assert low_metrics["open"] == 1
    assert low_metrics["resolved"] == 1
    assert low_metrics["breach_rate"] == 0.5
