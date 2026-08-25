import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.clock import now
from app.models.customer import Customer
from app.models.enums import (
    Category,
    CustomerTier,
    EventType,
    Priority,
    Role,
    TicketStatus,
)
from app.models.ticket import Ticket
from app.models.ticket_event import TicketEvent
from app.models.user import AppUser


async def create_agent(
    db_session: AsyncSession,
    email: str = "agent@example.com",
    is_active: bool = True,
    max_open_tickets: int | None = None,
) -> AppUser:
    user = AppUser(
        email=email,
        password_hash=security.get_password_hash("password"),
        name="Test Agent",
        role=Role.AGENT,
        is_active=is_active,
        max_open_tickets=max_open_tickets,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def create_dispatcher(db_session: AsyncSession, email: str = "disp@example.com") -> AppUser:
    user = AppUser(
        email=email,
        password_hash=security.get_password_hash("password"),
        name="Test Dispatcher",
        role=Role.DISPATCHER,
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def create_customer(db_session: AsyncSession, email: str = "cust@example.com") -> Customer:
    customer = Customer(
        email=email,
        password_hash=security.get_password_hash("password"),
        name="Test Cust",
        tier=CustomerTier.FREE,
    )
    db_session.add(customer)
    await db_session.commit()
    await db_session.refresh(customer)
    return customer


async def create_ticket(
    db_session: AsyncSession,
    customer_id: uuid.UUID,
    status: TicketStatus = TicketStatus.OPEN,
    assignee_id: uuid.UUID | None = None,
) -> Ticket:
    from sqlalchemy import text

    policy_id = (
        await db_session.execute(
            text("SELECT id FROM sla_policy_version ORDER BY activated_at DESC LIMIT 1")
        )
    ).scalar_one()

    ticket = Ticket(
        customer_id=customer_id,
        subject="Test Ticket",
        body="Issue",
        category=Category.GENERAL,
        priority=Priority.LOW,
        status=status,
        deadline=now(),
        sla_due_at=now(),
        sla_paused_at=now() if status == TicketStatus.PENDING_CUSTOMER else None,
        sla_policy_version_id=policy_id,
        assignee_id=assignee_id,
        created_at=now(),
        updated_at=now(),
    )
    db_session.add(ticket)
    await db_session.commit()
    await db_session.refresh(ticket)
    return ticket


async def get_auth_token(client: AsyncClient, email: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": "password"})
    return str(resp.json()["access_token"])


@pytest.mark.asyncio
@pytest.mark.db
async def test_inv_18_partial_failure(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """INV-18: One item's failure neither rolls back nor blocks the others."""
    dispatcher = await create_dispatcher(db_session)
    agent = await create_agent(db_session)
    customer = await create_customer(db_session)

    token = await get_auth_token(client, dispatcher.email)

    tickets = [
        await create_ticket(db_session, customer.id, status=TicketStatus.OPEN) for _ in range(10)
    ]
    ticket_ids = [str(t.id) for t in tickets]

    # Mock AssignmentService.assign_ticket to fail on the 5th item
    from unittest.mock import patch

    import app.services.assignment_service

    original_assign = app.services.assignment_service.AssignmentService.assign_ticket

    call_count = 0

    async def mock_assign(self, principal, ticket_id, assignee_id, override_capacity=False):
        nonlocal call_count
        call_count += 1
        if call_count == 5:
            raise RuntimeError("Unexpected failure")
        return await original_assign(
            self, principal, ticket_id, assignee_id, override_capacity=override_capacity
        )

    with patch("app.services.bulk_service.AssignmentService.assign_ticket", new=mock_assign):
        response = await client.post(
            "/api/v1/tickets/bulk/assignment",
            json={"ticket_ids": ticket_ids, "assignee_id": str(agent.id)},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["requested"] == 10
    assert data["succeeded"] == 9
    assert data["failed"] == 1

    results = data["results"]
    assert results[4]["ok"] is False
    assert results[4]["error"]["code"] == "INTERNAL_ERROR"

    # Check DB - 9 tickets assigned, 1 not
    assigned = (
        await db_session.execute(
            select(func.count(Ticket.id)).where(Ticket.assignee_id == agent.id)
        )
    ).scalar_one()
    assert assigned == 9

    # INV-5: Events written for the 9 successful ones
    events = (
        await db_session.execute(
            select(func.count(TicketEvent.id)).where(
                TicketEvent.ticket_id.in_(ticket_ids), TicketEvent.type == EventType.ASSIGNMENT
            )
        )
    ).scalar_one()
    assert events == 9


@pytest.mark.asyncio
@pytest.mark.db
async def test_bulk_assign_capacity(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    dispatcher = await create_dispatcher(db_session)
    agent = await create_agent(db_session, max_open_tickets=10)
    customer = await create_customer(db_session)

    token = await get_auth_token(client, dispatcher.email)

    tickets = [
        await create_ticket(db_session, customer.id, status=TicketStatus.OPEN) for _ in range(30)
    ]
    ticket_ids = [str(t.id) for t in tickets]

    response = await client.post(
        "/api/v1/tickets/bulk/assignment",
        json={"ticket_ids": ticket_ids, "assignee_id": str(agent.id)},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["requested"] == 30
    assert data["succeeded"] == 10
    assert data["failed"] == 20

    for i in range(10, 30):
        assert data["results"][i]["error"]["code"] == "BUSINESS_RULE_VIOLATION"


@pytest.mark.asyncio
@pytest.mark.db
async def test_bulk_reassign_statuses(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    dispatcher = await create_dispatcher(db_session)
    agent1 = await create_agent(db_session, email="agent1@example.com")
    agent2 = await create_agent(db_session, email="agent2@example.com")
    customer = await create_customer(db_session)

    token = await get_auth_token(client, dispatcher.email)

    # Create tickets in different statuses assigned to agent1
    t1 = await create_ticket(
        db_session, customer.id, status=TicketStatus.OPEN, assignee_id=agent1.id
    )
    t2 = await create_ticket(
        db_session, customer.id, status=TicketStatus.IN_PROGRESS, assignee_id=agent1.id
    )
    # Created for their side effect: the reassignment must have a
    # PENDING_CUSTOMER and a RESOLVED row present to leave alone.
    await create_ticket(
        db_session, customer.id, status=TicketStatus.PENDING_CUSTOMER, assignee_id=agent1.id
    )
    await create_ticket(
        db_session, customer.id, status=TicketStatus.RESOLVED, assignee_id=agent1.id
    )

    response = await client.post(
        "/api/v1/tickets/bulk/reassignment",
        json={
            "from_assignee_id": str(agent1.id),
            "to_assignee_id": str(agent2.id),
            "statuses": ["OPEN", "IN_PROGRESS"],
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["requested"] == 2
    assert data["succeeded"] == 2

    # Verify DB
    agent2_tickets = (
        (await db_session.execute(select(Ticket.id).where(Ticket.assignee_id == agent2.id)))
        .scalars()
        .all()
    )

    assert len(agent2_tickets) == 2
    assert t1.id in agent2_tickets
    assert t2.id in agent2_tickets
