"""List filters and keyset pagination (docs/API.md §1, §4)."""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.api.test_tickets import (
    create_admin,
    create_customer,
    get_auth_token,
    seed_priority_rules,
)


async def _make_tickets(client: AsyncClient, token: str, count: int) -> None:
    for index in range(count):
        response = await client.post(
            "/api/v1/tickets",
            json={"subject": f"T{index}", "body": "B", "category": "GENERAL"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 201


@pytest.mark.asyncio
@pytest.mark.db
async def test_cursor_walks_every_ticket_exactly_once(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    customer = await create_customer(db_session, "pg_c@example.com")
    await seed_priority_rules(db_session)
    token = await get_auth_token(client, customer.email)
    await _make_tickets(client, token, 5)

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):  # generous bound; the loop must terminate well before this
        url = f"/api/v1/tickets?limit=2{f'&cursor={cursor}' if cursor else ''}"
        page = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        assert page.status_code == 200
        body = page.json()
        seen.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert cursor is None, "pagination did not terminate"
    assert len(seen) == 5
    assert len(set(seen)) == 5, "a ticket was repeated across pages"


@pytest.mark.asyncio
@pytest.mark.db
async def test_last_page_reports_no_next_cursor(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    customer = await create_customer(db_session, "pg_c2@example.com")
    await seed_priority_rules(db_session)
    token = await get_auth_token(client, customer.email)
    await _make_tickets(client, token, 2)

    page = await client.get(
        "/api/v1/tickets?limit=50", headers={"Authorization": f"Bearer {token}"}
    )
    assert page.json()["next_cursor"] is None


@pytest.mark.asyncio
@pytest.mark.db
async def test_status_filter(client: AsyncClient, db_session: AsyncSession) -> None:
    customer = await create_customer(db_session, "pg_c3@example.com")
    admin = await create_admin(db_session, "pg_a3@example.com")
    await seed_priority_rules(db_session)
    token_c = await get_auth_token(client, customer.email)
    token_a = await get_auth_token(client, admin.email)
    await _make_tickets(client, token_c, 3)

    all_open = await client.get(
        "/api/v1/tickets?status=OPEN", headers={"Authorization": f"Bearer {token_a}"}
    )
    assert len(all_open.json()["items"]) == 3

    none_closed = await client.get(
        "/api/v1/tickets?status=CLOSED", headers={"Authorization": f"Bearer {token_a}"}
    )
    assert none_closed.json()["items"] == []


@pytest.mark.asyncio
@pytest.mark.db
async def test_breached_filter(client: AsyncClient, db_session: AsyncSession) -> None:
    customer = await create_customer(db_session, "pg_c4@example.com")
    admin = await create_admin(db_session, "pg_a4@example.com")
    await seed_priority_rules(db_session)
    token_c = await get_auth_token(client, customer.email)
    token_a = await get_auth_token(client, admin.email)
    await _make_tickets(client, token_c, 2)

    not_breached = await client.get(
        "/api/v1/tickets?breached=false", headers={"Authorization": f"Bearer {token_a}"}
    )
    assert len(not_breached.json()["items"]) == 2

    breached = await client.get(
        "/api/v1/tickets?breached=true", headers={"Authorization": f"Bearer {token_a}"}
    )
    assert breached.json()["items"] == []


@pytest.mark.asyncio
@pytest.mark.db
async def test_malformed_cursor_is_a_validation_error(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    customer = await create_customer(db_session, "pg_c5@example.com")
    await seed_priority_rules(db_session)
    token = await get_auth_token(client, customer.email)

    response = await client.get(
        "/api/v1/tickets?cursor=not-a-real-cursor",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
@pytest.mark.db
async def test_pagination_keeps_customer_isolation(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Paging must not become a way around INV-9."""
    owner = await create_customer(db_session, "pg_own@example.com")
    other = await create_customer(db_session, "pg_oth@example.com")
    await seed_priority_rules(db_session)
    token_own = await get_auth_token(client, owner.email)
    token_oth = await get_auth_token(client, other.email)
    await _make_tickets(client, token_own, 3)

    page = await client.get(
        "/api/v1/tickets?limit=50", headers={"Authorization": f"Bearer {token_oth}"}
    )
    assert page.json()["items"] == []
