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

    # 2. Customer cannot post comments
    resp_c = await client.post(
        f"/api/v1/tickets/{ticket_id}/comments",
        json={"type": CommentType.PUBLIC_REPLY, "body": "Hello"},
        headers={"Authorization": f"Bearer {token_c}"},
    )
    assert resp_c.status_code == 403

    # 3. Agent can post INTERNAL_NOTE and PUBLIC_REPLY
    resp_a1 = await client.post(
        f"/api/v1/tickets/{ticket_id}/comments",
        json={"type": CommentType.INTERNAL_NOTE, "body": "Secret note"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp_a1.status_code == 201

    resp_a2 = await client.post(
        f"/api/v1/tickets/{ticket_id}/comments",
        json={"type": CommentType.PUBLIC_REPLY, "body": "Public note"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp_a2.status_code == 201

    # 4. Agent sees both comments
    resp_get_a = await client.get(
        f"/api/v1/tickets/{ticket_id}/comments",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp_get_a.status_code == 200
    assert len(resp_get_a.json()["items"]) == 2

    # 5. Customer only sees PUBLIC_REPLY (INV-9)
    resp_get_c = await client.get(
        f"/api/v1/tickets/{ticket_id}/comments",
        headers={"Authorization": f"Bearer {token_c}"},
    )
    assert resp_get_c.status_code == 200
    items = resp_get_c.json()["items"]
    assert len(items) == 1
    assert items[0]["type"] == CommentType.PUBLIC_REPLY
    assert items[0]["body"] == "Public note"
