"""Claim, capacity and assignment configuration over HTTP (spec07 §8)."""

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.api.test_tickets import (
    create_admin,
    create_agent,
    create_customer,
    create_dispatcher,
    get_auth_token,
    seed_priority_rules,
)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _open_ticket(client: AsyncClient, token: str) -> str:
    resp = await client.post(
        "/api/v1/tickets",
        json={"subject": "S", "body": "B", "category": "GENERAL"},
        headers=auth(token),
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


@pytest.mark.db
async def test_agent_claims_an_unassigned_ticket(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    customer = await create_customer(db_session, "claim_c@example.com")
    agent = await create_agent(db_session, "claim_a@example.com")
    await seed_priority_rules(db_session)

    t_cust = await get_auth_token(client, customer.email)
    t_agent = await get_auth_token(client, agent.email)
    tid = await _open_ticket(client, t_cust)

    claimed = await client.post(f"/api/v1/tickets/{tid}/claim", headers=auth(t_agent))
    assert claimed.status_code == 200
    assert claimed.json()["id"] == tid


@pytest.mark.db
async def test_claiming_a_taken_ticket_is_409(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A lost race reads as information, not failure — the copy says so."""
    customer = await create_customer(db_session, "claim409_c@example.com")
    first = await create_agent(db_session, "claim409_a1@example.com")
    second = await create_agent(db_session, "claim409_a2@example.com")
    await seed_priority_rules(db_session)

    t_cust = await get_auth_token(client, customer.email)
    t_first = await get_auth_token(client, first.email)
    t_second = await get_auth_token(client, second.email)
    tid = await _open_ticket(client, t_cust)

    won = await client.post(f"/api/v1/tickets/{tid}/claim", headers=auth(t_first))
    assert won.status_code == 200
    lost = await client.post(f"/api/v1/tickets/{tid}/claim", headers=auth(t_second))
    assert lost.status_code == 409
    assert "took this one" in lost.json()["error"]["message"]


@pytest.mark.db
async def test_claim_matrix(client: AsyncClient, db_session: AsyncSession) -> None:
    """Agents and admins claim; customers and dispatchers do not (spec07 §7).

    A dispatcher already assigns to anyone including themselves, so claim would
    be a second spelling of an existing capability.
    """
    customer = await create_customer(db_session, "claimmx_c@example.com")
    dispatcher = await create_dispatcher(db_session, "claimmx_d@example.com")
    await seed_priority_rules(db_session)

    t_cust = await get_auth_token(client, customer.email)
    t_disp = await get_auth_token(client, dispatcher.email)
    tid = await _open_ticket(client, t_cust)

    for token in (t_cust, t_disp):
        refused = await client.post(f"/api/v1/tickets/{tid}/claim", headers=auth(token))
        assert refused.status_code == 403
    assert (await client.post(f"/api/v1/tickets/{tid}/claim")).status_code == 401


@pytest.mark.db
async def test_dispatcher_assign_respects_capacity_unless_overridden(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Capacity is a heuristic, not an invariant — the override is deliberate."""
    customer = await create_customer(db_session, "cap_c@example.com")
    agent = await create_agent(db_session, "cap_a@example.com")
    dispatcher = await create_dispatcher(db_session, "cap_d@example.com")
    await seed_priority_rules(db_session)
    await db_session.execute(
        text("UPDATE app_user SET max_open_tickets = 1 WHERE id = :uid"), {"uid": agent.id}
    )
    await db_session.commit()

    t_cust = await get_auth_token(client, customer.email)
    t_disp = await get_auth_token(client, dispatcher.email)

    first = await _open_ticket(client, t_cust)
    assert (
        await client.post(
            f"/api/v1/tickets/{first}/assignment",
            json={"assignee_id": str(agent.id)},
            headers=auth(t_disp),
        )
    ).status_code == 200

    second = await _open_ticket(client, t_cust)
    refused = await client.post(
        f"/api/v1/tickets/{second}/assignment",
        json={"assignee_id": str(agent.id)},
        headers=auth(t_disp),
    )
    assert refused.status_code == 422
    assert "ticket limit" in refused.json()["error"]["message"]

    forced = await client.post(
        f"/api/v1/tickets/{second}/assignment",
        json={"assignee_id": str(agent.id), "override_capacity": True},
        headers=auth(t_disp),
    )
    assert forced.status_code == 200


@pytest.mark.db
async def test_staff_directory_reports_workload(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """So the assign picker can show load and grey out full agents."""
    customer = await create_customer(db_session, "dir_c@example.com")
    agent = await create_agent(db_session, "dir_a@example.com")
    dispatcher = await create_dispatcher(db_session, "dir_d@example.com")
    await seed_priority_rules(db_session)

    t_cust = await get_auth_token(client, customer.email)
    t_agent = await get_auth_token(client, agent.email)
    t_disp = await get_auth_token(client, dispatcher.email)

    tid = await _open_ticket(client, t_cust)
    await client.post(f"/api/v1/tickets/{tid}/claim", headers=auth(t_agent))

    listing = await client.get("/api/v1/users?role=AGENT", headers=auth(t_disp))
    assert listing.status_code == 200
    row = next(u for u in listing.json()["items"] if u["id"] == str(agent.id))
    assert row["open_ticket_count"] == 1
    assert row["accepts_auto_assignment"] is True
    assert "password_hash" not in row


@pytest.mark.db
async def test_admin_sets_capacity_and_automation_opt_out(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """PATCH is partial: changing a ceiling must not reactivate someone."""
    agent = await create_agent(db_session, "patch_a@example.com")
    admin = await create_admin(db_session, "patch_adm@example.com")
    t_admin = await get_auth_token(client, admin.email)

    resp = await client.patch(
        f"/api/v1/users/{agent.id}",
        json={"max_open_tickets": 5, "accepts_auto_assignment": False},
        headers=auth(t_admin),
    )
    assert resp.status_code == 200
    assert resp.json()["max_open_tickets"] == 5
    assert resp.json()["accepts_auto_assignment"] is False
    assert resp.json()["is_active"] is True, "activation must be untouched"


@pytest.mark.db
async def test_assignment_configuration_is_admin_only(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    admin = await create_admin(db_session, "cfg_adm@example.com")
    dispatcher = await create_dispatcher(db_session, "cfg_d@example.com")
    t_admin = await get_auth_token(client, admin.email)
    t_disp = await get_auth_token(client, dispatcher.email)

    body = {"strategy": "ROUND_ROBIN", "auto_assign_on_create": True}
    assert (
        await client.put("/api/v1/configuration/assignment", json=body, headers=auth(t_disp))
    ).status_code == 403

    updated = await client.put("/api/v1/configuration/assignment", json=body, headers=auth(t_admin))
    assert updated.status_code == 200
    assert updated.json()["assignment"]["strategy"] == "ROUND_ROBIN"
    assert updated.json()["assignment"]["auto_assign_on_create"] is True


@pytest.mark.db
async def test_unknown_strategy_is_rejected(client: AsyncClient, db_session: AsyncSession) -> None:
    admin = await create_admin(db_session, "cfgbad_adm@example.com")
    t_admin = await get_auth_token(client, admin.email)

    resp = await client.put(
        "/api/v1/configuration/assignment",
        json={"strategy": "TELEPATHY", "auto_assign_on_create": True},
        headers=auth(t_admin),
    )
    assert resp.status_code == 400
