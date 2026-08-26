"""Deleting staff (docs/API.md §10).

Deletion is deliberately narrower than deactivation, and the tests here are
mostly about what it REFUSES. `ticket_event.actor_id` carries no foreign key —
it points into `app_user` or `customer` depending on `actor_type` — so the
database will not stop a delete that orphans the audit trail. The service check
is the only thing that does, which makes it worth pinning down properly.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.api.test_tickets import (
    create_admin,
    create_agent,
    create_customer,
    get_auth_token,
    seed_priority_rules,
)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _delete(client: AsyncClient, token: str, user_id: str) -> tuple[int, str]:
    resp = await client.delete(f"/api/v1/users/{user_id}", headers=auth(token))
    return resp.status_code, resp.text


@pytest.mark.db
async def test_an_unused_staff_row_can_be_deleted(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The reason this exists: a row created by a typo, five minutes ago."""
    admin = await create_admin(db_session, "del_adm@example.com")
    typo = await create_agent(db_session, "wrong-adress@example.com")
    token = await get_auth_token(client, admin.email)

    status, _ = await _delete(client, token, str(typo.id))
    assert status == 204

    listing = await client.get("/api/v1/users", headers=auth(token))
    assert str(typo.id) not in listing.text


@pytest.mark.db
async def test_deleting_is_idempotent_only_in_the_sense_that_gone_is_404(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    admin = await create_admin(db_session, "del_adm2@example.com")
    typo = await create_agent(db_session, "typo2@example.com")
    token = await get_auth_token(client, admin.email)

    assert (await _delete(client, token, str(typo.id)))[0] == 204
    assert (await _delete(client, token, str(typo.id)))[0] == 404


@pytest.mark.db
async def test_someone_who_holds_a_ticket_is_refused(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """**The audit-trail guard.**

    Not a foreign-key error dressed up: the refusal names what they touched and
    points at deactivation, because that is the action the admin actually wants.
    """
    admin = await create_admin(db_session, "hold_adm@example.com")
    agent = await create_agent(db_session, "hold_agent@example.com")
    customer = await create_customer(db_session, "hold_cust@example.com")
    await seed_priority_rules(db_session)

    t_admin = await get_auth_token(client, admin.email)
    t_cust = await get_auth_token(client, customer.email)

    created = await client.post(
        "/api/v1/tickets",
        json={"subject": "S", "body": "B", "category": "GENERAL"},
        headers=auth(t_cust),
    )
    await client.post(
        f"/api/v1/tickets/{created.json()['id']}/assignment",
        json={"assignee_id": str(agent.id)},
        headers=auth(t_admin),
    )

    status, body = await _delete(client, t_admin, str(agent.id))
    assert status == 422
    assert "Deactivate" in body, "the refusal must name the alternative"
    assert "ticket" in body.lower(), "and say what they touched"


@pytest.mark.db
async def test_someone_who_authored_an_audit_event_is_refused(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The case no foreign key covers.

    `ticket_event.actor_id` has no FK, so the database would let this through
    and leave the event attributed to an id resolving to nobody.
    """
    admin = await create_admin(db_session, "ev_adm@example.com")
    agent = await create_agent(db_session, "ev_agent@example.com")
    customer = await create_customer(db_session, "ev_cust@example.com")
    await seed_priority_rules(db_session)

    t_admin = await get_auth_token(client, admin.email)
    t_agent = await get_auth_token(client, agent.email)
    t_cust = await get_auth_token(client, customer.email)

    created = await client.post(
        "/api/v1/tickets",
        json={"subject": "S", "body": "B", "category": "GENERAL"},
        headers=auth(t_cust),
    )
    tid = created.json()["id"]
    await client.post(
        f"/api/v1/tickets/{tid}/assignment",
        json={"assignee_id": str(agent.id)},
        headers=auth(t_admin),
    )
    # The agent acts, writing a STATUS_CHANGE event with actor_id = agent.
    await client.post(
        f"/api/v1/tickets/{tid}/transitions", json={"to": "IN_PROGRESS"}, headers=auth(t_agent)
    )
    # Unassign so the ticket count alone would no longer block the delete.
    await client.post(
        f"/api/v1/tickets/{tid}/assignment",
        json={"assignee_id": str(admin.id)},
        headers=auth(t_admin),
    )

    status, body = await _delete(client, t_admin, str(agent.id))
    assert status == 422, "the audit trail must still hold them"
    assert "event" in body.lower()


@pytest.mark.db
async def test_an_admin_cannot_delete_themselves(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Lockout, not data integrity: the last admin deleting their own row has
    no way back in through the UI."""
    admin = await create_admin(db_session, "self_del@example.com")
    token = await get_auth_token(client, admin.email)

    status, body = await _delete(client, token, str(admin.id))
    assert status == 422
    assert "your own account" in body.lower()


@pytest.mark.db
async def test_deleting_is_admin_only(client: AsyncClient, db_session: AsyncSession) -> None:
    admin = await create_admin(db_session, "perm_adm@example.com")
    agent = await create_agent(db_session, "perm_agent@example.com")
    victim = await create_agent(db_session, "perm_victim@example.com")
    customer = await create_customer(db_session, "perm_cust@example.com")

    for principal in (agent, customer):
        token = await get_auth_token(client, principal.email)
        status, _ = await _delete(client, token, str(victim.id))
        assert status == 403, f"{principal.email} could delete staff"

    assert (await _delete(client, await get_auth_token(client, admin.email), str(victim.id)))[
        0
    ] == 204


@pytest.mark.db
async def test_deleting_requires_authentication(client: AsyncClient) -> None:
    invented = "00000000-0000-4000-8000-000000000000"
    assert (await client.delete(f"/api/v1/users/{invented}")).status_code == 401
