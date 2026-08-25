import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import CommentType
from tests.api.test_tickets import (
    create_agent,
    create_customer,
    get_auth_token,
    seed_priority_rules,
)


@pytest.fixture
async def setup_ticket(client: AsyncClient, db_session: AsyncSession) -> tuple[str, str, str]:
    customer = await create_customer(db_session, "commenter@example.com")
    agent = await create_agent(db_session, "agent_c@example.com")
    await seed_priority_rules(db_session)

    token_c = await get_auth_token(client, customer.email)
    token_a = await get_auth_token(client, agent.email)

    resp = await client.post(
        "/api/v1/tickets",
        json={"subject": "S", "body": "B", "category": "GENERAL"},
        headers={"Authorization": f"Bearer {token_c}"},
    )
    ticket_id = resp.json()["id"]

    # Assign to agent
    await client.post(
        f"/api/v1/tickets/{ticket_id}/assignment",
        json={"assignee_id": str(agent.id)},
        headers={"Authorization": f"Bearer {token_a}"},  # agent is dispatcher/admin for assignment?
        # wait, AGENT can't assign! let's just create a dispatcher.
    )

    return ticket_id, token_c, token_a


@pytest.mark.asyncio
@pytest.mark.db
async def test_comments_visibility(client: AsyncClient, db_session: AsyncSession) -> None:
    # 1. Setup ticket
    customer = await create_customer(db_session, "comment_cust@example.com")
    agent = await create_agent(db_session, "comment_agent@example.com")

    from tests.api.test_tickets import create_dispatcher

    dispatcher_user = await create_dispatcher(db_session, "real_disp@example.com")

    await seed_priority_rules(db_session)

    token_c = await get_auth_token(client, customer.email)
    token_a = await get_auth_token(client, agent.email)
    token_d = await get_auth_token(client, dispatcher_user.email)

    resp = await client.post(
        "/api/v1/tickets",
        json={"subject": "S", "body": "B", "category": "GENERAL"},
        headers={"Authorization": f"Bearer {token_c}"},
    )
    ticket_id = resp.json()["id"]

    # assign to agent so they have visibility
    await client.post(
        f"/api/v1/tickets/{ticket_id}/assignment",
        json={"assignee_id": str(agent.id)},
        headers={"Authorization": f"Bearer {token_d}"},
    )

    # 2. Customer can post PUBLIC_REPLY to their own ticket
    resp_c = await client.post(
        f"/api/v1/tickets/{ticket_id}/comments",
        json={"type": CommentType.PUBLIC_REPLY, "body": "Hello from customer"},
        headers={"Authorization": f"Bearer {token_c}"},
    )
    assert resp_c.status_code == 201
    assert resp_c.json()["author"]["type"] == "CUSTOMER"

    # 3. Customer cannot post INTERNAL_NOTE
    resp_c2 = await client.post(
        f"/api/v1/tickets/{ticket_id}/comments",
        json={"type": CommentType.INTERNAL_NOTE, "body": "Sneaky note"},
        headers={"Authorization": f"Bearer {token_c}"},
    )
    assert resp_c2.status_code == 403

    # 4. Agent can post INTERNAL_NOTE and PUBLIC_REPLY
    resp_a1 = await client.post(
        f"/api/v1/tickets/{ticket_id}/comments",
        json={"type": CommentType.INTERNAL_NOTE, "body": "Secret note"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp_a1.status_code == 201
    assert resp_a1.json()["author"]["type"] == "USER"

    resp_a2 = await client.post(
        f"/api/v1/tickets/{ticket_id}/comments",
        json={"type": CommentType.PUBLIC_REPLY, "body": "Public note"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp_a2.status_code == 201

    # 5. Agent sees all comments
    resp_get_a = await client.get(
        f"/api/v1/tickets/{ticket_id}/comments",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp_get_a.status_code == 200
    items = resp_get_a.json()["items"]
    assert len(items) == 3
    assert items[0]["author"]["type"] == "CUSTOMER"
    assert items[1]["author"]["type"] == "USER"

    # 6. Customer only sees PUBLIC_REPLY (INV-9 regression)
    resp_get_c = await client.get(
        f"/api/v1/tickets/{ticket_id}/comments",
        headers={"Authorization": f"Bearer {token_c}"},
    )
    assert resp_get_c.status_code == 200
    items = resp_get_c.json()["items"]
    assert len(items) == 2
    assert items[0]["type"] == CommentType.PUBLIC_REPLY
    assert items[0]["body"] == "Hello from customer"
    assert items[1]["type"] == CommentType.PUBLIC_REPLY
    assert items[1]["body"] == "Public note"

    # 7. Customer replies to another customer's ticket -> 404
    customer2 = await create_customer(db_session, "comment_cust2@example.com")
    token_c2 = await get_auth_token(client, customer2.email)
    resp_c3 = await client.post(
        f"/api/v1/tickets/{ticket_id}/comments",
        json={"type": CommentType.PUBLIC_REPLY, "body": "Interloper"},
        headers={"Authorization": f"Bearer {token_c2}"},
    )
    assert resp_c3.status_code == 404

    # 8. CLOSED ticket rejects comments

    await client.post(
        f"/api/v1/tickets/{ticket_id}/transitions",
        json={"to": "IN_PROGRESS"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    await client.post(
        f"/api/v1/tickets/{ticket_id}/transitions",
        json={"to": "RESOLVED"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    await client.post(
        f"/api/v1/tickets/{ticket_id}/transitions",
        json={"to": "CLOSED"},
        headers={"Authorization": f"Bearer {token_d}"},
    )
    resp_closed = await client.post(
        f"/api/v1/tickets/{ticket_id}/comments",
        json={"type": CommentType.PUBLIC_REPLY, "body": "Too late"},
        headers={"Authorization": f"Bearer {token_c}"},
    )
    assert resp_closed.status_code == 422
