"""Direct coverage for invariants that had no home (docs/TESTING.md §2).

INV-1  priority and deadline never change after creation.
INV-10 ticket_event rows are never updated or deleted.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.sla import duration
from app.models.enums import EventType, Priority, TicketStatus
from app.models.ticket import Ticket
from app.models.ticket_event import TicketEvent
from app.repositories.event_repo import TicketEventRepository
from tests.api.test_tickets import (
    create_admin,
    create_agent,
    create_customer,
    get_auth_token,
    seed_priority_rules,
)


@pytest.mark.asyncio
@pytest.mark.db
async def test_inv10_audit_rows_cannot_be_updated(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The append-only rule makes UPDATE a silent no-op rather than a change."""
    customer = await create_customer(db_session, "inv_c@example.com")
    await seed_priority_rules(db_session)
    token = await get_auth_token(client, customer.email)

    created = await client.post(
        "/api/v1/tickets",
        json={"subject": "S", "body": "B", "category": "GENERAL"},
        headers={"Authorization": f"Bearer {token}"},
    )
    ticket_id = uuid.UUID(created.json()["id"])

    event = (
        await db_session.execute(select(TicketEvent).where(TicketEvent.ticket_id == ticket_id))
    ).scalar_one()
    original_type = event.type

    await db_session.execute(
        update(TicketEvent).where(TicketEvent.id == event.id).values(type=EventType.SLA_BREACH)
    )
    await db_session.commit()

    # Read back through raw SQL so no identity-map cache can mask a real change.
    after_type = (
        await db_session.execute(
            text("SELECT type::text FROM ticket_event WHERE id = :eid"), {"eid": event.id}
        )
    ).scalar_one()
    assert after_type == original_type.value, "audit row was mutated (INV-10)"


@pytest.mark.asyncio
@pytest.mark.db
async def test_inv10_audit_rows_cannot_be_deleted(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    customer = await create_customer(db_session, "inv_c2@example.com")
    await seed_priority_rules(db_session)
    token = await get_auth_token(client, customer.email)

    created = await client.post(
        "/api/v1/tickets",
        json={"subject": "S", "body": "B", "category": "GENERAL"},
        headers={"Authorization": f"Bearer {token}"},
    )
    ticket_id = uuid.UUID(created.json()["id"])

    before = (
        await db_session.execute(
            text("SELECT count(*) FROM ticket_event WHERE ticket_id = :tid"),
            {"tid": ticket_id},
        )
    ).scalar_one()
    assert before >= 1

    await db_session.execute(delete(TicketEvent).where(TicketEvent.ticket_id == ticket_id))
    await db_session.commit()

    after = (
        await db_session.execute(
            text("SELECT count(*) FROM ticket_event WHERE ticket_id = :tid"),
            {"tid": ticket_id},
        )
    ).scalar_one()
    assert after == before, "audit rows were deleted (INV-10)"


def test_inv10_event_repo_exposes_no_mutation_path() -> None:
    """App-layer half of INV-10: there is no update/delete method to call."""
    surface = {name for name in dir(TicketEventRepository) if not name.startswith("_")}
    assert surface == {"insert", "list_by_ticket"}, (
        f"event_repo must expose insert/select only; found {sorted(surface)}"
    )


@pytest.mark.asyncio
@pytest.mark.db
async def test_inv1_priority_and_deadline_never_change(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Drive a ticket through its whole lifecycle; the SLA terms must not move."""
    customer = await create_customer(db_session, "inv_c3@example.com")
    admin = await create_admin(db_session, "inv_a3@example.com")
    agent = await create_agent(db_session, "inv_ag3@example.com")
    await seed_priority_rules(db_session)
    token_c = await get_auth_token(client, customer.email)
    token_a = await get_auth_token(client, admin.email)

    created = await client.post(
        "/api/v1/tickets",
        json={"subject": "S", "body": "B", "category": "OUTAGE"},
        headers={"Authorization": f"Bearer {token_c}"},
    )
    body = created.json()
    ticket_id = body["id"]
    original_priority, original_deadline = body["priority"], body["deadline"]

    await client.post(
        f"/api/v1/tickets/{ticket_id}/assignment",
        json={"assignee_id": str(agent.id)},
        headers={"Authorization": f"Bearer {token_a}"},
    )

    for to_status in (TicketStatus.IN_PROGRESS, TicketStatus.RESOLVED, TicketStatus.CLOSED):
        response = await client.post(
            f"/api/v1/tickets/{ticket_id}/transitions",
            json={"to": to_status},
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert response.status_code == 200
        assert response.json()["priority"] == original_priority, "priority changed (INV-1)"
        assert response.json()["deadline"] == original_deadline, "deadline changed (INV-1)"

    # INV-2 as well: the stored deadline still equals created_at + duration.
    ticket = (
        await db_session.execute(select(Ticket).where(Ticket.id == uuid.UUID(ticket_id)))
    ).scalar_one()
    expected = duration(Priority(original_priority))
    assert ticket.deadline - ticket.created_at == expected, (
        "deadline must still equal created_at + duration(priority) (INV-2)"
    )
