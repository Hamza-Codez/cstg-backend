"""Lifecycle v2 over HTTP (spec05 §11).

The narrow widening of AUTHORIZATION.md §3: a customer may answer a question
addressed to them (T5 resume, T6 reopen) and may still never assert that work
was done (T1 start, T2 resolve, T3 close).
"""

from typing import Any

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


async def _started_ticket(
    client: AsyncClient, db_session: AsyncSession, prefix: str
) -> tuple[str, str, str, str]:
    """An IN_PROGRESS ticket; returns (ticket_id, customer token, agent token, admin token)."""
    customer = await create_customer(db_session, f"{prefix}_c@example.com")
    agent = await create_agent(db_session, f"{prefix}_a@example.com")
    admin = await create_admin(db_session, f"{prefix}_adm@example.com")
    await seed_priority_rules(db_session)

    t_cust = await get_auth_token(client, customer.email)
    t_agent = await get_auth_token(client, agent.email)
    t_admin = await get_auth_token(client, admin.email)

    created = await client.post(
        "/api/v1/tickets",
        json={"subject": "S", "body": "B", "category": "GENERAL"},
        headers=auth(t_cust),
    )
    ticket_id = created.json()["id"]

    await client.post(
        f"/api/v1/tickets/{ticket_id}/assignment",
        json={"assignee_id": str(agent.id)},
        headers=auth(t_admin),
    )
    started = await client.post(
        f"/api/v1/tickets/{ticket_id}/transitions",
        json={"to": "IN_PROGRESS"},
        headers=auth(t_agent),
    )
    assert started.status_code == 200, started.text
    return ticket_id, t_cust, t_agent, t_admin


async def _to(client: AsyncClient, tid: str, token: str, status: str) -> Any:
    return await client.post(
        f"/api/v1/tickets/{tid}/transitions", json={"to": status}, headers=auth(token)
    )


@pytest.mark.db
async def test_agent_pauses_and_customer_resumes(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    tid, t_cust, t_agent, _ = await _started_ticket(client, db_session, "pauseflow")

    paused = await _to(client, tid, t_agent, "PENDING_CUSTOMER")
    assert paused.status_code == 200
    assert paused.json()["status"] == "PENDING_CUSTOMER"
    assert paused.json()["sla_paused_at"] is not None

    # The customer answers the question the pause asked.
    resumed = await _to(client, tid, t_cust, "IN_PROGRESS")
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "IN_PROGRESS"
    assert resumed.json()["sla_paused_at"] is None


@pytest.mark.db
async def test_customer_cannot_pause(client: AsyncClient, db_session: AsyncSession) -> None:
    """T4 is the assigned agent's call — the desk decides it is blocked."""
    tid, t_cust, _, _ = await _started_ticket(client, db_session, "custpause")
    assert (await _to(client, tid, t_cust, "PENDING_CUSTOMER")).status_code == 403


@pytest.mark.db
async def test_customer_still_cannot_drive_the_work_transitions(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The v1 rule, narrowed rather than abandoned — a P16 regression guard."""
    tid, t_cust, t_agent, _ = await _started_ticket(client, db_session, "custwork")

    assert (await _to(client, tid, t_cust, "RESOLVED")).status_code == 403

    await _to(client, tid, t_agent, "RESOLVED")
    assert (await _to(client, tid, t_cust, "CLOSED")).status_code == 403


@pytest.mark.db
async def test_customer_reopens_their_own_resolved_ticket(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    tid, t_cust, t_agent, _ = await _started_ticket(client, db_session, "reopenown")
    await _to(client, tid, t_agent, "RESOLVED")

    reopened = await _to(client, tid, t_cust, "IN_PROGRESS")
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "IN_PROGRESS"
    assert reopened.json()["reopen_count"] == 1
    assert reopened.json()["resolved_at"] is None


@pytest.mark.db
async def test_another_customer_cannot_reopen(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Object-level narrowing: CUSTOMER in the table means the OWNING customer.

    404 rather than 403 — a stranger must not learn the ticket exists (INV-9).
    """
    tid, _, t_agent, _ = await _started_ticket(client, db_session, "reopenother")
    stranger = await create_customer(db_session, "reopen_stranger@example.com")
    t_stranger = await get_auth_token(client, stranger.email)

    await _to(client, tid, t_agent, "RESOLVED")
    assert (await _to(client, tid, t_stranger, "IN_PROGRESS")).status_code == 404


@pytest.mark.db
async def test_dispatcher_may_reopen_but_not_pause(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    tid, _, t_agent, _ = await _started_ticket(client, db_session, "dispreopen")
    dispatcher = await create_dispatcher(db_session, "disp_reopen@example.com")
    t_disp = await get_auth_token(client, dispatcher.email)

    assert (await _to(client, tid, t_disp, "PENDING_CUSTOMER")).status_code == 403

    await _to(client, tid, t_agent, "RESOLVED")
    assert (await _to(client, tid, t_disp, "IN_PROGRESS")).status_code == 200


@pytest.mark.db
async def test_reopen_outside_the_window_is_refused(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Unbounded reopen would let an old ticket breach against an old deadline."""
    tid, t_cust, t_agent, _ = await _started_ticket(client, db_session, "reopenwindow")
    await _to(client, tid, t_agent, "RESOLVED")

    # Age the resolution past APP_REOPEN_WINDOW_DAYS (default 14).
    await db_session.execute(
        text("UPDATE ticket SET resolved_at = resolved_at - interval '30 days' WHERE id = :tid"),
        {"tid": tid},
    )
    await db_session.commit()

    refused = await _to(client, tid, t_cust, "IN_PROGRESS")
    assert refused.status_code == 422
    assert "new one" in refused.json()["error"]["message"]


@pytest.mark.db
async def test_closed_stays_terminal(client: AsyncClient, db_session: AsyncSession) -> None:
    """There is exactly one way out of RESOLVED besides closing, and none out of CLOSED."""
    tid, t_cust, t_agent, _ = await _started_ticket(client, db_session, "closedterm")
    await _to(client, tid, t_agent, "RESOLVED")
    await _to(client, tid, t_agent, "CLOSED")

    for actor in (t_cust, t_agent):
        assert (await _to(client, tid, actor, "IN_PROGRESS")).status_code == 409


@pytest.mark.db
async def test_customer_reply_auto_resumes_the_clock(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """T5 auto-resume (spec05 §4).

    The clock restarts when the customer responds, not when an agent notices
    they did — that is the whole point of the state.
    """
    tid, t_cust, t_agent, _ = await _started_ticket(client, db_session, "autoresume")
    await _to(client, tid, t_agent, "PENDING_CUSTOMER")

    replied = await client.post(
        f"/api/v1/tickets/{tid}/comments",
        json={"type": "PUBLIC_REPLY", "body": "Here is the log file you asked for."},
        headers=auth(t_cust),
    )
    assert replied.status_code == 201

    detail = await client.get(f"/api/v1/tickets/{tid}", headers=auth(t_cust))
    assert detail.json()["status"] == "IN_PROGRESS", "the reply should have resumed the clock"
    assert detail.json()["sla_paused_at"] is None


@pytest.mark.db
async def test_staff_reply_does_not_resume(client: AsyncClient, db_session: AsyncSession) -> None:
    """Only the customer's answer lifts the block; an agent's note does not."""
    tid, _, t_agent, _ = await _started_ticket(client, db_session, "staffreply")
    await _to(client, tid, t_agent, "PENDING_CUSTOMER")

    await client.post(
        f"/api/v1/tickets/{tid}/comments",
        json={"type": "INTERNAL_NOTE", "body": "Chasing the customer."},
        headers=auth(t_agent),
    )

    detail = await client.get(f"/api/v1/tickets/{tid}", headers=auth(t_agent))
    assert detail.json()["status"] == "PENDING_CUSTOMER"


@pytest.mark.db
async def test_pause_is_illegal_from_open(client: AsyncClient, db_session: AsyncSession) -> None:
    """T4 starts from IN_PROGRESS: you cannot wait on a customer before starting."""
    customer = await create_customer(db_session, "pauseopen_c@example.com")
    agent = await create_agent(db_session, "pauseopen_a@example.com")
    admin = await create_admin(db_session, "pauseopen_adm@example.com")
    await seed_priority_rules(db_session)
    t_cust = await get_auth_token(client, customer.email)
    t_agent = await get_auth_token(client, agent.email)
    t_admin = await get_auth_token(client, admin.email)

    created = await client.post(
        "/api/v1/tickets",
        json={"subject": "S", "body": "B", "category": "GENERAL"},
        headers=auth(t_cust),
    )
    tid = created.json()["id"]

    # Assigned but not started, so the agent can SEE the ticket — otherwise the
    # 404 that hides it would fire before legality is ever considered, and this
    # test would pass for the wrong reason.
    await client.post(
        f"/api/v1/tickets/{tid}/assignment",
        json={"assignee_id": str(agent.id)},
        headers=auth(t_admin),
    )

    assert (await _to(client, tid, t_agent, "PENDING_CUSTOMER")).status_code == 409


@pytest.mark.db
async def test_response_exposes_both_the_promise_and_the_effective_due_time(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """`deadline` stays the frozen promise; `sla_due_at` is what a countdown reads."""
    tid, t_cust, _, _ = await _started_ticket(client, db_session, "duefields")
    body = (await client.get(f"/api/v1/tickets/{tid}", headers=auth(t_cust))).json()

    assert body["deadline"] == body["sla_due_at"], "equal until something pauses"
    assert body["sla_paused_seconds"] == 0
    assert body["reopen_count"] == 0
