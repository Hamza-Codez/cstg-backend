"""Attachment API contract (spec03 §9).

The organising rule is INV-12: an attachment is readable exactly when its parent
ticket is readable, and writable exactly when the principal may act on that
ticket. There is no attachment-level permission, so every case below is really a
statement about the parent.
"""

import io

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


def _file(name: str = "test.txt", body: bytes = b"Hello world", ctype: str = "text/plain") -> dict:
    return {"file": (name, io.BytesIO(body), ctype)}


async def _open_ticket(client: AsyncClient, token: str) -> str:
    resp = await client.post(
        "/api/v1/tickets",
        json={"subject": "S", "body": "B", "category": "GENERAL"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    return str(resp.json()["id"])


@pytest.mark.db
async def test_customer_uploads_to_own_ticket_and_staff_can_read_it(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """P14 opens upload to customers — AUTHORIZATION.md §3 previously said "v2"."""
    customer = await create_customer(db_session, "att_cust@example.com")
    agent = await create_agent(db_session, "att_agent@example.com")
    dispatcher = await create_dispatcher(db_session, "att_disp@example.com")
    await seed_priority_rules(db_session)

    token_c = await get_auth_token(client, customer.email)
    token_a = await get_auth_token(client, agent.email)
    token_d = await get_auth_token(client, dispatcher.email)

    ticket_id = await _open_ticket(client, token_c)
    await client.post(
        f"/api/v1/tickets/{ticket_id}/assignment",
        json={"assignee_id": str(agent.id)},
        headers={"Authorization": f"Bearer {token_d}"},
    )

    resp = await client.post(
        f"/api/v1/tickets/{ticket_id}/attachments",
        files=_file(),
        headers={"Authorization": f"Bearer {token_c}"},
    )
    assert resp.status_code == 201
    attachment_id = resp.json()["id"]
    assert "storage_path" not in resp.json(), "internal storage keys must not cross the API"

    # The assigned agent reads the same object through the same ticket.
    dl = await client.get(
        f"/api/v1/tickets/{ticket_id}/attachments/{attachment_id}",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert dl.status_code == 200
    assert dl.content == b"Hello world"
    assert dl.headers["content-disposition"].startswith("attachment;")


@pytest.mark.db
async def test_inv12_access_mirrors_the_parent_ticket(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """INV-12: no attachment path grants what the parent ticket does not.

    Two principals who cannot see the ticket — a different customer, and an
    agent it is not assigned to — get 404 on upload, list, and download alike.
    Existence is hidden, so it is 404 and never 403.
    """
    owner = await create_customer(db_session, "inv12_owner@example.com")
    stranger = await create_customer(db_session, "inv12_stranger@example.com")
    unassigned_agent = await create_agent(db_session, "inv12_agent@example.com")
    await seed_priority_rules(db_session)

    token_owner = await get_auth_token(client, owner.email)
    token_stranger = await get_auth_token(client, stranger.email)
    token_agent = await get_auth_token(client, unassigned_agent.email)

    ticket_id = await _open_ticket(client, token_owner)
    created = await client.post(
        f"/api/v1/tickets/{ticket_id}/attachments",
        files=_file(),
        headers={"Authorization": f"Bearer {token_owner}"},
    )
    attachment_id = created.json()["id"]

    for label, token in (("other customer", token_stranger), ("unassigned agent", token_agent)):
        upload = await client.post(
            f"/api/v1/tickets/{ticket_id}/attachments",
            files=_file(),
            headers={"Authorization": f"Bearer {token}"},
        )
        assert upload.status_code == 404, f"{label} upload"

        listing = await client.get(
            f"/api/v1/tickets/{ticket_id}/attachments",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert listing.status_code == 404, f"{label} list"

        download = await client.get(
            f"/api/v1/tickets/{ticket_id}/attachments/{attachment_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert download.status_code == 404, f"{label} download"


@pytest.mark.db
async def test_list_returns_attachments_for_a_readable_ticket(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    customer = await create_customer(db_session, "att_list@example.com")
    await seed_priority_rules(db_session)
    token = await get_auth_token(client, customer.email)
    ticket_id = await _open_ticket(client, token)

    empty = await client.get(
        f"/api/v1/tickets/{ticket_id}/attachments",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert empty.status_code == 200
    assert empty.json()["items"] == []

    for name in ("a.txt", "b.txt"):
        await client.post(
            f"/api/v1/tickets/{ticket_id}/attachments",
            files=_file(name=name),
            headers={"Authorization": f"Bearer {token}"},
        )

    listing = await client.get(
        f"/api/v1/tickets/{ticket_id}/attachments",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert listing.status_code == 200
    assert [item["filename"] for item in listing.json()["items"]] == ["a.txt", "b.txt"]


@pytest.mark.db
async def test_download_with_mismatched_ticket_and_attachment_is_404(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The nested route must not lie about its own shape (spec03 §7).

    Both tickets belong to the same customer, so this is not an authorization
    failure — the pair simply does not exist, and v1 did not verify it.
    """
    customer = await create_customer(db_session, "att_pair@example.com")
    await seed_priority_rules(db_session)
    token = await get_auth_token(client, customer.email)

    ticket_a = await _open_ticket(client, token)
    ticket_b = await _open_ticket(client, token)

    created = await client.post(
        f"/api/v1/tickets/{ticket_a}/attachments",
        files=_file(),
        headers={"Authorization": f"Bearer {token}"},
    )
    attachment_id = created.json()["id"]

    mismatched = await client.get(
        f"/api/v1/tickets/{ticket_b}/attachments/{attachment_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert mismatched.status_code == 404


@pytest.mark.db
async def test_disallowed_content_type_is_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    customer = await create_customer(db_session, "att_ctype@example.com")
    await seed_priority_rules(db_session)
    token = await get_auth_token(client, customer.email)
    ticket_id = await _open_ticket(client, token)

    resp = await client.post(
        f"/api/v1/tickets/{ticket_id}/attachments",
        files=_file(name="evil.exe", ctype="application/x-msdownload"),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "BUSINESS_RULE_VIOLATION"


@pytest.mark.db
async def test_upload_to_a_closed_ticket_is_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Matches the rule spec02 set for replies: a closed ticket takes nothing new."""
    customer = await create_customer(db_session, "att_closed@example.com")
    agent = await create_agent(db_session, "att_closed_agent@example.com")
    dispatcher = await create_dispatcher(db_session, "att_closed_disp@example.com")
    await seed_priority_rules(db_session)

    token_c = await get_auth_token(client, customer.email)
    token_a = await get_auth_token(client, agent.email)
    token_d = await get_auth_token(client, dispatcher.email)

    ticket_id = await _open_ticket(client, token_c)
    await client.post(
        f"/api/v1/tickets/{ticket_id}/assignment",
        json={"assignee_id": str(agent.id)},
        headers={"Authorization": f"Bearer {token_d}"},
    )
    for to in ("IN_PROGRESS", "RESOLVED", "CLOSED"):
        resp = await client.post(
            f"/api/v1/tickets/{ticket_id}/transitions",
            json={"to": to},
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert resp.status_code == 200, f"transition to {to}: {resp.text}"

    blocked = await client.post(
        f"/api/v1/tickets/{ticket_id}/attachments",
        files=_file(),
        headers={"Authorization": f"Bearer {token_c}"},
    )
    assert blocked.status_code == 422
