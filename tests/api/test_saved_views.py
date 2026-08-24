"""Saved views (spec04 §8).

Private to their owner: another owner's id is indistinguishable from one that
never existed, so it is 404 and never 403.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.api.test_tickets import (
    create_agent,
    create_customer,
    create_dispatcher,
    get_auth_token,
    seed_priority_rules,
)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.db
async def test_create_list_and_delete_a_view(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    dispatcher = await create_dispatcher(db_session, "sv_disp@example.com")
    await seed_priority_rules(db_session)
    token = await get_auth_token(client, dispatcher.email)

    created = await client.post(
        "/api/v1/saved-views",
        json={"name": "Overdue enterprise", "filters": {"breached": True, "tier": "ENTERPRISE"}},
        headers=auth(token),
    )
    assert created.status_code == 201, created.text
    view_id = created.json()["id"]
    assert created.json()["filters"]["tier"] == "ENTERPRISE"

    listing = await client.get("/api/v1/saved-views", headers=auth(token))
    assert [v["name"] for v in listing.json()["items"]] == ["Overdue enterprise"]

    deleted = await client.delete(f"/api/v1/saved-views/{view_id}", headers=auth(token))
    assert deleted.status_code == 204

    empty = await client.get("/api/v1/saved-views", headers=auth(token))
    assert empty.json()["items"] == []


@pytest.mark.db
async def test_views_are_private_to_their_owner(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    owner = await create_dispatcher(db_session, "sv_owner@example.com")
    other = await create_dispatcher(db_session, "sv_other@example.com")
    await seed_priority_rules(db_session)

    t_owner = await get_auth_token(client, owner.email)
    t_other = await get_auth_token(client, other.email)

    created = await client.post(
        "/api/v1/saved-views",
        json={"name": "Mine", "filters": {"status": "OPEN"}},
        headers=auth(t_owner),
    )
    view_id = created.json()["id"]

    # Not visible in another user's listing...
    assert (await client.get("/api/v1/saved-views", headers=auth(t_other))).json()["items"] == []

    # ...and not deletable by them. 404, not 403 — existence is not disclosed.
    forbidden = await client.delete(f"/api/v1/saved-views/{view_id}", headers=auth(t_other))
    assert forbidden.status_code == 404

    # Still there for the owner.
    still_there = await client.get("/api/v1/saved-views", headers=auth(t_owner))
    assert len(still_there.json()["items"]) == 1


@pytest.mark.db
async def test_duplicate_name_is_rejected(client: AsyncClient, db_session: AsyncSession) -> None:
    dispatcher = await create_dispatcher(db_session, "sv_dup@example.com")
    await seed_priority_rules(db_session)
    token = await get_auth_token(client, dispatcher.email)

    body = {"name": "Same", "filters": {"status": "OPEN"}}
    first = await client.post("/api/v1/saved-views", json=body, headers=auth(token))
    assert first.status_code == 201
    duplicate = await client.post("/api/v1/saved-views", json=body, headers=auth(token))
    assert duplicate.status_code == 422


@pytest.mark.db
async def test_saving_a_filter_your_role_may_not_use_is_refused(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Validated at write time against the author's current role (spec04 §6).

    Without this an agent could persist a dispatcher-only filter and replay it
    later.
    """
    agent = await create_agent(db_session, "sv_agent@example.com")
    await seed_priority_rules(db_session)
    token = await get_auth_token(client, agent.email)

    refused = await client.post(
        "/api/v1/saved-views",
        json={"name": "Everyone's work", "filters": {"assignee_id": str(uuid.uuid4())}},
        headers=auth(token),
    )
    assert refused.status_code == 403


@pytest.mark.db
async def test_unknown_filter_key_is_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    dispatcher = await create_dispatcher(db_session, "sv_extra@example.com")
    await seed_priority_rules(db_session)
    token = await get_auth_token(client, dispatcher.email)

    resp = await client.post(
        "/api/v1/saved-views",
        json={"name": "Typo", "filters": {"statuss": "OPEN"}},
        headers=auth(token),
    )
    assert resp.status_code == 400


@pytest.mark.db
async def test_customers_have_no_saved_views(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    customer = await create_customer(db_session, "sv_cust@example.com")
    await seed_priority_rules(db_session)
    token = await get_auth_token(client, customer.email)

    # The coarse role gate refuses before the service is reached.
    assert (await client.get("/api/v1/saved-views", headers=auth(token))).status_code == 403
