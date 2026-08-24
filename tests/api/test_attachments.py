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


@pytest.mark.asyncio
@pytest.mark.db
async def test_attachments(client: AsyncClient, db_session: AsyncSession) -> None:
    customer = await create_customer(db_session, "att_cust@example.com")
    agent = await create_agent(db_session, "att_agent@example.com")
    dispatcher = await create_dispatcher(db_session, "att_disp@example.com")
    await seed_priority_rules(db_session)

    token_c = await get_auth_token(client, customer.email)
    token_a = await get_auth_token(client, agent.email)
    token_d = await get_auth_token(client, dispatcher.email)

    resp = await client.post(
        "/api/v1/tickets",
        json={"subject": "S", "body": "B", "category": "GENERAL"},
        headers={"Authorization": f"Bearer {token_c}"},
    )
    ticket_id = resp.json()["id"]

    await client.post(
        f"/api/v1/tickets/{ticket_id}/assignment",
        json={"assignee_id": str(agent.id)},
        headers={"Authorization": f"Bearer {token_d}"},
    )

    # 1. Customer cannot upload attachments in v1
    file_data = {"file": ("test.txt", io.BytesIO(b"Hello world"), "text/plain")}
    resp_upload_c = await client.post(
        f"/api/v1/tickets/{ticket_id}/attachments",
        files=file_data,
        headers={"Authorization": f"Bearer {token_c}"},
    )
    # Matrix: CUSTOMER is ✗ for upload in v1 — a capability they do not have,
    # so this is 403 rather than existence-hiding 404.
    assert resp_upload_c.status_code == 403

    # 2. Agent can upload
    file_data = {"file": ("test.txt", io.BytesIO(b"Hello world"), "text/plain")}
    resp_upload_a = await client.post(
        f"/api/v1/tickets/{ticket_id}/attachments",
        files=file_data,
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp_upload_a.status_code == 201
    attachment_id = resp_upload_a.json()["id"]

    # 3. Agent can download
    resp_dl_a = await client.get(
        f"/api/v1/tickets/{ticket_id}/attachments/{attachment_id}",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp_dl_a.status_code == 200
    assert resp_dl_a.content == b"Hello world"

    # 4. Customer can download (since they can view ticket)
    resp_dl_c = await client.get(
        f"/api/v1/tickets/{ticket_id}/attachments/{attachment_id}",
        headers={"Authorization": f"Bearer {token_c}"},
    )
    assert resp_dl_c.status_code == 200
    assert resp_dl_c.content == b"Hello world"
