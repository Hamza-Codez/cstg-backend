"""Notification history, dismissal and clear-all over HTTP (P21).

Separate from `test_notifications.py` because the properties are different in
kind: that file pins down **who may be notified** (INV-17), this one pins down
**what a recipient may do to their own feed** without touching the audit log or
anyone else's view.

The load-bearing test here is `test_inv9_dismissal_is_not_an_existence_oracle`.
Dismissal is the first endpoint in this API that takes a `ticket_event` id
straight from the client, and the natural implementation — insert the row, return
204 — turns it into a probe for the existence of arbitrary events.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.api.test_notifications import _count, _feed, _open_ticket, auth
from tests.api.test_tickets import (
    create_admin,
    create_agent,
    create_customer,
    get_auth_token,
    seed_priority_rules,
)

INVENTED = "00000000-0000-4000-8000-000000000000"


async def _dismiss(client: AsyncClient, token: str, event_id: str) -> int:
    resp = await client.delete(f"/api/v1/notifications/{event_id}", headers=auth(token))
    return resp.status_code


async def _assign(client: AsyncClient, admin_token: str, ticket_id: str, agent_id: str) -> None:
    resp = await client.post(
        f"/api/v1/tickets/{ticket_id}/assignment",
        json={"assignee_id": agent_id},
        headers=auth(admin_token),
    )
    assert resp.status_code == 200, resp.text


# ── The feed is a history ──────────────────────────────────────────────────────


@pytest.mark.db
async def test_reading_no_longer_empties_the_feed(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """**The P21 behaviour change.**

    The list used to filter on the read cursor, so opening the bell marked
    everything read and the next open showed nothing. A panel that empties the
    moment you look at it cannot hold a history, which is what made dismissal
    and a notifications page impossible to build on top of it.
    """
    customer = await create_customer(db_session, "hist_c@example.com")
    agent = await create_agent(db_session, "hist_a@example.com")
    admin = await create_admin(db_session, "hist_adm@example.com")
    await seed_priority_rules(db_session)

    t_cust = await get_auth_token(client, customer.email)
    t_agent = await get_auth_token(client, agent.email)
    t_admin = await get_auth_token(client, admin.email)

    tid = await _open_ticket(client, t_cust, "Still here after reading")
    await _assign(client, t_admin, tid, str(agent.id))
    await client.post(
        f"/api/v1/tickets/{tid}/comments",
        json={"type": "PUBLIC_REPLY", "body": "Looking into it."},
        headers=auth(t_agent),
    )

    before = await _feed(client, t_cust)
    assert len(before["items"]) >= 1
    assert before["unread_count"] >= 1
    assert all(item["read"] is False for item in before["items"])

    await client.post("/api/v1/notifications/read", json={}, headers=auth(t_cust))

    after = await _feed(client, t_cust)
    assert len(after["items"]) == len(before["items"]), "the history survives being read"
    assert after["unread_count"] == 0, "but the badge clears"
    assert all(item["read"] is True for item in after["items"])


# ── Dismissal ──────────────────────────────────────────────────────────────────


@pytest.mark.db
async def test_dismissing_hides_it_for_me_and_nobody_else(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The whole point of per-principal dismissal.

    The audit event is not deletable at all — migration 0006 makes DELETE on
    `ticket_event` a silent no-op — and a shared delete would let one person
    erase another person's notification.
    """
    customer = await create_customer(db_session, "dis_c@example.com")
    agent = await create_agent(db_session, "dis_a@example.com")
    admin = await create_admin(db_session, "dis_adm@example.com")
    await seed_priority_rules(db_session)

    t_cust = await get_auth_token(client, customer.email)
    t_agent = await get_auth_token(client, agent.email)
    t_admin = await get_auth_token(client, admin.email)

    tid = await _open_ticket(client, t_cust, "Shared ticket")
    await _assign(client, t_admin, tid, str(agent.id))

    # Intersect rather than taking the newest of either feed: an actor never
    # sees their own events, so the agent's top item is the admin's assignment
    # and the admin's is the customer's CREATED. The overlap is what both can
    # see — here, the CREATED event neither of them caused.
    agent_ids = {i["event_id"] for i in (await _feed(client, t_agent))["items"]}
    admin_ids = {i["event_id"] for i in (await _feed(client, t_admin))["items"]}
    shared = next(iter(agent_ids & admin_ids))

    assert await _dismiss(client, t_agent, shared) == 204

    assert shared not in {i["event_id"] for i in (await _feed(client, t_agent))["items"]}
    assert shared in {i["event_id"] for i in (await _feed(client, t_admin))["items"]}, (
        "one dismissal must not reach into another feed"
    )


@pytest.mark.db
async def test_a_dismissal_survives_repeated_reads(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """It is a row, not client state, so it outlives the tab."""
    customer = await create_customer(db_session, "surv_c@example.com")
    agent = await create_agent(db_session, "surv_a@example.com")
    admin = await create_admin(db_session, "surv_adm@example.com")
    await seed_priority_rules(db_session)
    t_cust = await get_auth_token(client, customer.email)
    t_admin = await get_auth_token(client, admin.email)

    tid = await _open_ticket(client, t_cust)
    await _assign(client, t_admin, tid, str(agent.id))

    event_id = (await _feed(client, t_admin))["items"][0]["event_id"]
    assert await _dismiss(client, t_admin, event_id) == 204

    for _ in range(3):
        assert event_id not in {i["event_id"] for i in (await _feed(client, t_admin))["items"]}


@pytest.mark.db
async def test_dismissing_an_unread_one_drops_the_badge(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A dismissed notification cannot be unread, because it is not visible."""
    customer = await create_customer(db_session, "badge_c@example.com")
    agent = await create_agent(db_session, "badge_a@example.com")
    admin = await create_admin(db_session, "badge_adm@example.com")
    await seed_priority_rules(db_session)
    t_cust = await get_auth_token(client, customer.email)
    t_admin = await get_auth_token(client, admin.email)

    tid = await _open_ticket(client, t_cust)
    await _assign(client, t_admin, tid, str(agent.id))

    before = await _count(client, t_admin)
    assert before >= 1
    event_id = (await _feed(client, t_admin))["items"][0]["event_id"]
    await _dismiss(client, t_admin, event_id)

    assert await _count(client, t_admin) == before - 1


@pytest.mark.db
async def test_dismissing_twice_is_success_not_an_error(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A double-click, or a retry after a dropped response, is the expected
    case — not a failure. Without the idempotency check the second call would
    404, because the first one made the event invisible to the lookup."""
    customer = await create_customer(db_session, "idem_c@example.com")
    agent = await create_agent(db_session, "idem_a@example.com")
    admin = await create_admin(db_session, "idem_adm@example.com")
    await seed_priority_rules(db_session)
    t_cust = await get_auth_token(client, customer.email)
    t_admin = await get_auth_token(client, admin.email)

    tid = await _open_ticket(client, t_cust)
    await _assign(client, t_admin, tid, str(agent.id))
    event_id = (await _feed(client, t_admin))["items"][0]["event_id"]

    assert await _dismiss(client, t_admin, event_id) == 204
    assert await _dismiss(client, t_admin, event_id) == 204


# ── INV-9: dismissal must not leak existence ───────────────────────────────────


@pytest.mark.db
async def test_inv9_dismissal_is_not_an_existence_oracle(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """**The security test for this phase.**

    If "not yours" answered differently from "does not exist", anyone could
    enumerate event ids by watching the status codes. Both must be 404, and the
    bodies must match too — a differing message leaks just as much.
    """
    alice = await create_customer(db_session, "orc_alice@example.com")
    bob = await create_customer(db_session, "orc_bob@example.com")
    agent = await create_agent(db_session, "orc_agent@example.com")
    admin = await create_admin(db_session, "orc_adm@example.com")
    await seed_priority_rules(db_session)

    t_alice = await get_auth_token(client, alice.email)
    t_bob = await get_auth_token(client, bob.email)
    t_agent = await get_auth_token(client, agent.email)
    t_admin = await get_auth_token(client, admin.email)

    bobs = await _open_ticket(client, t_bob, "A private problem")
    await _assign(client, t_admin, bobs, str(agent.id))
    await client.post(
        f"/api/v1/tickets/{bobs}/comments",
        json={"type": "PUBLIC_REPLY", "body": "On it."},
        headers=auth(t_agent),
    )

    real_event = (await _feed(client, t_bob))["items"][0]["event_id"]

    someone_elses = await client.delete(
        f"/api/v1/notifications/{real_event}", headers=auth(t_alice)
    )
    nonexistent = await client.delete(f"/api/v1/notifications/{INVENTED}", headers=auth(t_alice))

    assert someone_elses.status_code == 404, "403 here would confirm the event exists"
    assert nonexistent.status_code == 404
    assert someone_elses.json() == nonexistent.json(), "the bodies must not differ either"

    # And it really was not dismissed for its owner.
    assert real_event in {i["event_id"] for i in (await _feed(client, t_bob))["items"]}


@pytest.mark.db
async def test_inv9_a_customer_cannot_dismiss_an_internal_note_event(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The note is invisible to them, so its id must be too — otherwise a 204
    would confirm that an internal note exists and when it was written."""
    customer = await create_customer(db_session, "note_d@example.com")
    agent = await create_agent(db_session, "note_da@example.com")
    admin = await create_admin(db_session, "note_dadm@example.com")
    await seed_priority_rules(db_session)
    t_cust = await get_auth_token(client, customer.email)
    t_agent = await get_auth_token(client, agent.email)
    t_admin = await get_auth_token(client, admin.email)

    tid = await _open_ticket(client, t_cust)
    await _assign(client, t_admin, tid, str(agent.id))
    await client.post(
        f"/api/v1/tickets/{tid}/comments",
        json={"type": "INTERNAL_NOTE", "body": "A difficult conversation."},
        headers=auth(t_agent),
    )

    # Read the id from the *admin* feed, not the agent's: the agent wrote the
    # note, and an actor never sees their own events.
    note_event = next(
        i["event_id"] for i in (await _feed(client, t_admin))["items"] if i["type"] == "COMMENT"
    )
    assert await _dismiss(client, t_cust, note_event) == 404


# ── Clear all ──────────────────────────────────────────────────────────────────


@pytest.mark.db
async def test_clear_all_empties_the_feed_but_not_the_future(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    customer = await create_customer(db_session, "clr_c@example.com")
    agent = await create_agent(db_session, "clr_a@example.com")
    admin = await create_admin(db_session, "clr_adm@example.com")
    await seed_priority_rules(db_session)
    t_cust = await get_auth_token(client, customer.email)
    t_agent = await get_auth_token(client, agent.email)
    t_admin = await get_auth_token(client, admin.email)

    tid = await _open_ticket(client, t_cust, "Before the clear")
    await _assign(client, t_admin, tid, str(agent.id))
    assert (await _feed(client, t_admin))["items"] != []

    cleared = await client.post("/api/v1/notifications/clear", json={}, headers=auth(t_admin))
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["unread_count"] == 0
    assert (await _feed(client, t_admin))["items"] == []

    # A floor, not an off switch: new events still arrive.
    await client.post(
        f"/api/v1/tickets/{tid}/transitions", json={"to": "IN_PROGRESS"}, headers=auth(t_agent)
    )
    after = await _feed(client, t_admin)
    assert len(after["items"]) == 1
    assert after["unread_count"] == 1


@pytest.mark.db
async def test_clearing_twice_does_not_resurrect_anything(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """`cleared_before` moves forward only, like the read cursor.

    GREATEST over a NULL yields NULL in PostgreSQL, which would silently undo
    the clear on the second call — hence the COALESCE in the upsert. This is
    the test that fails if that is removed.
    """
    customer = await create_customer(db_session, "fwd2_c@example.com")
    agent = await create_agent(db_session, "fwd2_a@example.com")
    admin = await create_admin(db_session, "fwd2_adm@example.com")
    await seed_priority_rules(db_session)
    t_cust = await get_auth_token(client, customer.email)
    t_admin = await get_auth_token(client, admin.email)

    tid = await _open_ticket(client, t_cust)
    await _assign(client, t_admin, tid, str(agent.id))

    await client.post("/api/v1/notifications/clear", json={}, headers=auth(t_admin))
    assert (await _feed(client, t_admin))["items"] == []

    await client.post("/api/v1/notifications/clear", json={}, headers=auth(t_admin))
    assert (await _feed(client, t_admin))["items"] == [], "still empty, never resurrected"


@pytest.mark.db
async def test_clearing_is_per_principal(client: AsyncClient, db_session: AsyncSession) -> None:
    customer = await create_customer(db_session, "clrp_c@example.com")
    agent = await create_agent(db_session, "clrp_a@example.com")
    admin = await create_admin(db_session, "clrp_adm@example.com")
    await seed_priority_rules(db_session)
    t_cust = await get_auth_token(client, customer.email)
    t_agent = await get_auth_token(client, agent.email)
    t_admin = await get_auth_token(client, admin.email)

    tid = await _open_ticket(client, t_cust, "Shared again")
    await _assign(client, t_admin, tid, str(agent.id))
    assert (await _feed(client, t_agent))["items"] != []

    await client.post("/api/v1/notifications/clear", json={}, headers=auth(t_admin))

    assert (await _feed(client, t_admin))["items"] == []
    assert (await _feed(client, t_agent))["items"] != [], "the agent feed is untouched"


# ── Paging ─────────────────────────────────────────────────────────────────────


@pytest.mark.db
async def test_paging_neither_skips_nor_repeats(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Keyset over `(created_at, id)`.

    Events written in one transaction share a timestamp to the microsecond, so
    ordering on `created_at` alone would be unstable and a cursor could skip or
    repeat a row at the page boundary.
    """
    customer = await create_customer(db_session, "pg_c@example.com")
    agent = await create_agent(db_session, "pg_a@example.com")
    admin = await create_admin(db_session, "pg_adm@example.com")
    await seed_priority_rules(db_session)
    t_cust = await get_auth_token(client, customer.email)
    t_admin = await get_auth_token(client, admin.email)

    for n in range(7):
        tid = await _open_ticket(client, t_cust, f"Ticket {n}")
        await _assign(client, t_admin, tid, str(agent.id))

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(12):
        url = "/api/v1/notifications?limit=3"
        if cursor:
            url += f"&cursor={cursor}"
        page = (await client.get(url, headers=auth(t_admin))).json()
        seen.extend(i["event_id"] for i in page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break

    assert cursor is None, "the walk terminated rather than running out of iterations"
    assert len(seen) == len(set(seen)), "no event repeated across pages"

    everything = await client.get("/api/v1/notifications?limit=50", headers=auth(t_admin))
    assert set(seen) == {i["event_id"] for i in everything.json()["items"]}, "none skipped"


@pytest.mark.db
async def test_a_malformed_cursor_is_400(client: AsyncClient, db_session: AsyncSession) -> None:
    admin = await create_admin(db_session, "cur_adm@example.com")
    token = await get_auth_token(client, admin.email)
    resp = await client.get("/api/v1/notifications?cursor=not-a-cursor", headers=auth(token))
    assert resp.status_code == 400


@pytest.mark.db
async def test_the_new_paths_require_authentication(client: AsyncClient) -> None:
    assert (await client.delete(f"/api/v1/notifications/{INVENTED}")).status_code == 401
    assert (await client.post("/api/v1/notifications/clear", json={})).status_code == 401
