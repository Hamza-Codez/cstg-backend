"""Search, filters and their authorization (spec04 §8).

Search is the single most likely place to leak ticket existence, because
relevance ranking naturally wants to run before authorization. The scope-first
rule is enforced in the repository; these tests are the proof.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.api.test_tickets import (
    create_admin,
    create_agent,
    create_customer,
    create_dispatcher,
    get_auth_token,
    seed_priority_rules,
)


async def _open_ticket(client: AsyncClient, token: str, subject: str, body: str) -> str:
    resp = await client.post(
        "/api/v1/tickets",
        json={"subject": subject, "body": body, "category": "GENERAL"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _search(client: AsyncClient, token: str, query: str) -> dict:
    resp = await client.get(
        "/api/v1/tickets", params={"q": query}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.db
async def test_inv9_search_never_crosses_customer_boundaries(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """**The critical test.** Customer A searches a term that appears only in
    customer B's ticket.

    The answer must be an empty list — not a 403 (which confirms the term
    matched something), and not a count that includes invisible rows.
    """
    alice = await create_customer(db_session, "search_alice@example.com")
    bob = await create_customer(db_session, "search_bob@example.com")
    await seed_priority_rules(db_session)

    t_alice = await get_auth_token(client, alice.email)
    t_bob = await get_auth_token(client, bob.email)

    await _open_ticket(client, t_bob, "Zarquon deployment failure", "The zarquon cluster is down")
    await _open_ticket(client, t_alice, "Password reset", "I cannot sign in")

    leaked = await _search(client, t_alice, "zarquon")
    assert leaked["items"] == []
    assert leaked["next_cursor"] is None

    # Bob, who owns it, finds it — proving the term really does match and the
    # empty result above was scope, not a broken query.
    found = await _search(client, t_bob, "zarquon")
    assert [item["subject"] for item in found["items"]] == ["Zarquon deployment failure"]


@pytest.mark.db
async def test_agent_search_is_limited_to_assigned_tickets(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    customer = await create_customer(db_session, "search_cust2@example.com")
    agent = await create_agent(db_session, "search_agent@example.com")
    admin = await create_admin(db_session, "search_admin@example.com")
    await seed_priority_rules(db_session)

    t_cust = await get_auth_token(client, customer.email)
    t_agent = await get_auth_token(client, agent.email)
    t_admin = await get_auth_token(client, admin.email)

    assigned = await _open_ticket(client, t_cust, "Kryptonite leak", "kryptonite everywhere")
    await _open_ticket(client, t_cust, "Kryptonite spill", "more kryptonite")

    await client.post(
        f"/api/v1/tickets/{assigned}/assignment",
        json={"assignee_id": str(agent.id)},
        headers={"Authorization": f"Bearer {t_admin}"},
    )

    agent_hits = await _search(client, t_agent, "kryptonite")
    assert [item["id"] for item in agent_hits["items"]] == [assigned]

    # Admin sees both — the scope differs, the query does not.
    admin_hits = await _search(client, t_admin, "kryptonite")
    assert len(admin_hits["items"]) == 2


@pytest.mark.db
async def test_subject_match_outranks_body_match(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Weighting works: setweight A on subject, B on body (spec04 §2)."""
    customer = await create_customer(db_session, "search_rank@example.com")
    await seed_priority_rules(db_session)
    token = await get_auth_token(client, customer.email)

    body_only = await _open_ticket(client, token, "General question", "the invoice was wrong")
    subject_hit = await _open_ticket(client, token, "Invoice is wrong", "please look into this")

    results = await _search(client, token, "invoice")
    ids = [item["id"] for item in results["items"]]
    assert ids == [subject_hit, body_only], "a title match must outrank a passing mention"


@pytest.mark.db
async def test_role_forbidden_filters_are_refused_not_ignored(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """403, never a silently narrowed result set (spec04 §4)."""
    alice = await create_customer(db_session, "filt_alice@example.com")
    bob = await create_customer(db_session, "filt_bob@example.com")
    agent = await create_agent(db_session, "filt_agent@example.com")
    await seed_priority_rules(db_session)

    t_alice = await get_auth_token(client, alice.email)
    t_agent = await get_auth_token(client, agent.email)

    for params in (
        {"customer_id": str(bob.id)},
        {"assignee_id": str(agent.id)},
        {"tier": "ENTERPRISE"},
    ):
        resp = await client.get(
            "/api/v1/tickets", params=params, headers={"Authorization": f"Bearer {t_alice}"}
        )
        assert resp.status_code == 403, f"{params} -> {resp.status_code}"
        assert resp.json()["error"]["code"] == "FORBIDDEN"

    # An agent is equally barred from the staff-wide filters.
    resp = await client.get(
        "/api/v1/tickets",
        params={"customer_id": str(bob.id)},
        headers={"Authorization": f"Bearer {t_agent}"},
    )
    assert resp.status_code == 403


@pytest.mark.db
async def test_dispatcher_may_use_staff_wide_filters(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    customer = await create_customer(db_session, "filt_cust@example.com")
    dispatcher = await create_dispatcher(db_session, "filt_disp@example.com")
    await seed_priority_rules(db_session)

    t_cust = await get_auth_token(client, customer.email)
    t_disp = await get_auth_token(client, dispatcher.email)
    await _open_ticket(client, t_cust, "Visible to dispatcher", "body")

    resp = await client.get(
        "/api/v1/tickets",
        params={"customer_id": str(customer.id)},
        headers={"Authorization": f"Bearer {t_disp}"},
    )
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 1


@pytest.mark.db
async def test_filters_compose_with_search(client: AsyncClient, db_session: AsyncSession) -> None:
    customer = await create_customer(db_session, "filt_compose@example.com")
    await seed_priority_rules(db_session)
    token = await get_auth_token(client, customer.email)

    await _open_ticket(client, token, "Widget broken", "the widget fell over")
    await _open_ticket(client, token, "Widget question", "how do I use the widget")

    both = await _search(client, token, "widget")
    assert len(both["items"]) == 2

    narrowed = await client.get(
        "/api/v1/tickets",
        params={"q": "widget", "status": "RESOLVED"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert narrowed.status_code == 200
    assert narrowed.json()["items"] == []


@pytest.mark.db
async def test_search_cursor_is_rejected_on_a_plain_list(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Changing query shape mid-pagination must fail loudly, not silently skip.

    This is what forces the frontend to drop `cursor` whenever a filter changes
    (spec04 frontend §5).
    """
    customer = await create_customer(db_session, "cursor_shape@example.com")
    await seed_priority_rules(db_session)
    token = await get_auth_token(client, customer.email)

    for index in range(3):
        await _open_ticket(client, token, f"Sprocket {index}", "sprocket trouble")

    page = await client.get(
        "/api/v1/tickets",
        params={"q": "sprocket", "limit": 1},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert page.status_code == 200
    search_cursor = page.json()["next_cursor"]
    assert search_cursor is not None

    reused = await client.get(
        "/api/v1/tickets",
        params={"cursor": search_cursor},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert reused.status_code == 400
    assert reused.json()["error"]["code"] == "VALIDATION_ERROR"
