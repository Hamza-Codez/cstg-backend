"""Notifications over HTTP (spec08 §9).

**INV-17** is an API-level property — a notification never reveals an event on a
ticket its recipient may not read — so it gets the weight here rather than in
the unit tier.
"""

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


async def _open_ticket(client: AsyncClient, token: str, subject: str = "S") -> str:
    resp = await client.post(
        "/api/v1/tickets",
        json={"subject": subject, "body": "B", "category": "GENERAL"},
        headers=auth(token),
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _feed(client: AsyncClient, token: str) -> dict:
    resp = await client.get("/api/v1/notifications", headers=auth(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _count(client: AsyncClient, token: str) -> int:
    resp = await client.get("/api/v1/notifications/count", headers=auth(token))
    assert resp.status_code == 200
    return int(resp.json()["unread_count"])


@pytest.mark.db
async def test_inv17_a_customer_is_never_notified_about_another_customers_ticket(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The cross-customer leak. Scope is computed at READ time from the shared
    predicate, so this cannot drift from ticket listing."""
    alice = await create_customer(db_session, "notif_alice@example.com")
    bob = await create_customer(db_session, "notif_bob@example.com")
    agent = await create_agent(db_session, "notif_agent@example.com")
    admin = await create_admin(db_session, "notif_admin@example.com")
    await seed_priority_rules(db_session)

    t_alice = await get_auth_token(client, alice.email)
    t_bob = await get_auth_token(client, bob.email)
    t_agent = await get_auth_token(client, agent.email)
    t_admin = await get_auth_token(client, admin.email)

    bobs = await _open_ticket(client, t_bob, "Bob's private problem")
    await client.post(
        f"/api/v1/tickets/{bobs}/assignment",
        json={"assignee_id": str(agent.id)},
        headers=auth(t_admin),
    )
    await client.post(
        f"/api/v1/tickets/{bobs}/transitions", json={"to": "IN_PROGRESS"}, headers=auth(t_agent)
    )

    feed = await _feed(client, t_alice)
    assert feed["items"] == []
    assert feed["unread_count"] == 0

    subjects = [i["ticket_subject"] for i in (await _feed(client, t_bob))["items"]]
    assert "Bob's private problem" in subjects, "the owner does see it"


@pytest.mark.db
async def test_inv17_a_customer_is_never_notified_about_an_internal_note(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """**The internal-note leak.**

    Not even a contentless notification: a badge appearing when a note is
    written leaks its existence and timing, which is exactly what INV-9 forbids.
    """
    customer = await create_customer(db_session, "note_c@example.com")
    agent = await create_agent(db_session, "note_a@example.com")
    admin = await create_admin(db_session, "note_adm@example.com")
    await seed_priority_rules(db_session)

    t_cust = await get_auth_token(client, customer.email)
    t_agent = await get_auth_token(client, agent.email)
    t_admin = await get_auth_token(client, admin.email)

    tid = await _open_ticket(client, t_cust)
    await client.post(
        f"/api/v1/tickets/{tid}/assignment",
        json={"assignee_id": str(agent.id)},
        headers=auth(t_admin),
    )
    await client.post("/api/v1/notifications/read", json={}, headers=auth(t_cust))
    assert await _count(client, t_cust) == 0

    note = await client.post(
        f"/api/v1/tickets/{tid}/comments",
        json={"type": "INTERNAL_NOTE", "body": "Customer is being difficult."},
        headers=auth(t_agent),
    )
    assert note.status_code == 201

    assert await _count(client, t_cust) == 0, "an internal note must not move the badge"
    assert (await _feed(client, t_cust))["items"] == []


@pytest.mark.db
async def test_a_customer_is_notified_about_a_public_reply(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The other half of the filter: replies must get through."""
    customer = await create_customer(db_session, "reply_c@example.com")
    agent = await create_agent(db_session, "reply_a@example.com")
    admin = await create_admin(db_session, "reply_adm@example.com")
    await seed_priority_rules(db_session)

    t_cust = await get_auth_token(client, customer.email)
    t_agent = await get_auth_token(client, agent.email)
    t_admin = await get_auth_token(client, admin.email)

    tid = await _open_ticket(client, t_cust)
    await client.post(
        f"/api/v1/tickets/{tid}/assignment",
        json={"assignee_id": str(agent.id)},
        headers=auth(t_admin),
    )
    await client.post("/api/v1/notifications/read", json={}, headers=auth(t_cust))

    await client.post(
        f"/api/v1/tickets/{tid}/comments",
        json={"type": "PUBLIC_REPLY", "body": "We're looking into it."},
        headers=auth(t_agent),
    )

    assert await _count(client, t_cust) == 1
    assert (await _feed(client, t_cust))["items"][0]["type"] == "COMMENT"


@pytest.mark.db
async def test_inv17_scope_is_dynamic_so_an_unassigned_agent_stops_seeing_it(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """**The dynamic-scope test.**

    Visibility is computed at read time, so notifications generated while an
    agent held a ticket disappear the moment they are unassigned. A
    row-per-recipient design would have to hunt those rows down and delete them.
    """
    customer = await create_customer(db_session, "dyn_c@example.com")
    first = await create_agent(db_session, "dyn_a1@example.com")
    second = await create_agent(db_session, "dyn_a2@example.com")
    admin = await create_admin(db_session, "dyn_adm@example.com")
    await seed_priority_rules(db_session)

    t_cust = await get_auth_token(client, customer.email)
    t_first = await get_auth_token(client, first.email)
    t_admin = await get_auth_token(client, admin.email)

    tid = await _open_ticket(client, t_cust, "Moves between agents")
    await client.post(
        f"/api/v1/tickets/{tid}/assignment",
        json={"assignee_id": str(first.id)},
        headers=auth(t_admin),
    )

    subjects = [i["ticket_subject"] for i in (await _feed(client, t_first))["items"]]
    assert "Moves between agents" in subjects

    # Reassigned away.
    await client.post(
        f"/api/v1/tickets/{tid}/assignment",
        json={"assignee_id": str(second.id)},
        headers=auth(t_admin),
    )

    subjects = [i["ticket_subject"] for i in (await _feed(client, t_first))["items"]]
    assert "Moves between agents" not in subjects


@pytest.mark.db
async def test_self_authored_events_are_excluded(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """ "You resolved a ticket" is not news."""
    customer = await create_customer(db_session, "self_c@example.com")
    await seed_priority_rules(db_session)
    t_cust = await get_auth_token(client, customer.email)

    await client.post("/api/v1/notifications/read", json={}, headers=auth(t_cust))
    await _open_ticket(client, t_cust)

    assert await _count(client, t_cust) == 0, "creating your own ticket is not a notification"


@pytest.mark.db
async def test_system_breach_events_do_notify(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The IS DISTINCT FROM case.

    SYSTEM events carry actor_id NULL. With `<>` instead, NULL <> :me is NULL and
    every breach notification would be silently dropped.
    """
    customer = await create_customer(db_session, "sys_c@example.com")
    await seed_priority_rules(db_session)
    t_cust = await get_auth_token(client, customer.email)

    tid = await _open_ticket(client, t_cust)
    await client.post("/api/v1/notifications/read", json={}, headers=auth(t_cust))

    # A SYSTEM-authored breach event, as the monitor writes it.
    await db_session.execute(
        text(
            "INSERT INTO ticket_event (id, ticket_id, type, actor_type, actor_id, created_at) "
            "VALUES (gen_random_uuid(), :tid, 'SLA_BREACH', 'SYSTEM', NULL, now())"
        ),
        {"tid": tid},
    )
    await db_session.commit()

    assert await _count(client, t_cust) == 1
    assert (await _feed(client, t_cust))["items"][0]["actor_name"] is None


@pytest.mark.db
async def test_marking_read_clears_the_badge_and_new_events_raise_it(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    customer = await create_customer(db_session, "read_c@example.com")
    agent = await create_agent(db_session, "read_a@example.com")
    admin = await create_admin(db_session, "read_adm@example.com")
    await seed_priority_rules(db_session)

    t_cust = await get_auth_token(client, customer.email)
    t_admin = await get_auth_token(client, admin.email)

    tid = await _open_ticket(client, t_cust)
    await client.post(
        f"/api/v1/tickets/{tid}/assignment",
        json={"assignee_id": str(agent.id)},
        headers=auth(t_admin),
    )

    marked = await client.post("/api/v1/notifications/read", json={}, headers=auth(t_cust))
    assert marked.status_code == 200
    assert marked.json()["unread_count"] == 0

    await client.post(
        f"/api/v1/tickets/{tid}/transitions", json={"to": "IN_PROGRESS"}, headers=auth(t_admin)
    )
    assert await _count(client, t_cust) == 1


@pytest.mark.db
async def test_the_cursor_only_moves_forward(client: AsyncClient, db_session: AsyncSession) -> None:
    """A client must not resurrect old notifications by sending a past time."""
    customer = await create_customer(db_session, "fwd_c@example.com")
    agent = await create_agent(db_session, "fwd_a@example.com")
    admin = await create_admin(db_session, "fwd_adm@example.com")
    await seed_priority_rules(db_session)

    t_cust = await get_auth_token(client, customer.email)
    t_admin = await get_auth_token(client, admin.email)

    tid = await _open_ticket(client, t_cust)
    await client.post(
        f"/api/v1/tickets/{tid}/assignment",
        json={"assignee_id": str(agent.id)},
        headers=auth(t_admin),
    )
    await client.post("/api/v1/notifications/read", json={}, headers=auth(t_cust))
    assert await _count(client, t_cust) == 0

    rewound = await client.post(
        "/api/v1/notifications/read",
        json={"up_to": "2020-01-01T00:00:00Z"},
        headers=auth(t_cust),
    )
    assert rewound.status_code == 200
    assert await _count(client, t_cust) == 0, "the cursor must not have moved back"


@pytest.mark.db
async def test_a_dispatcher_sees_every_ticket_s_events(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice = await create_customer(db_session, "disp_alice@example.com")
    bob = await create_customer(db_session, "disp_bob@example.com")
    dispatcher = await create_dispatcher(db_session, "disp_d@example.com")
    await seed_priority_rules(db_session)

    t_alice = await get_auth_token(client, alice.email)
    t_bob = await get_auth_token(client, bob.email)
    t_disp = await get_auth_token(client, dispatcher.email)

    await _open_ticket(client, t_alice, "Alice ticket")
    await _open_ticket(client, t_bob, "Bob ticket")

    subjects = {i["ticket_subject"] for i in (await _feed(client, t_disp))["items"]}
    assert {"Alice ticket", "Bob ticket"} <= subjects


@pytest.mark.db
async def test_notifications_require_authentication(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/notifications")).status_code == 401
    assert (await client.get("/api/v1/notifications/count")).status_code == 401
    assert (await client.post("/api/v1/notifications/read", json={})).status_code == 401
