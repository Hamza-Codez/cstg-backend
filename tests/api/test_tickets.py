import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.models.customer import Customer
from app.models.enums import CustomerTier, Role, TicketStatus
from app.models.priority_rule import PriorityRule
from app.models.ticket import Ticket
from app.models.ticket_event import TicketEvent
from app.models.user import AppUser


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


async def create_agent(
    db_session: AsyncSession, email: str = "agent@example.com", *, is_active: bool = True
) -> AppUser:
    user = AppUser(
        email=email,
        password_hash=security.get_password_hash("password"),
        name="Test Agent",
        role=Role.AGENT,
        is_active=is_active,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def create_admin(db_session: AsyncSession, email: str = "admin@example.com") -> AppUser:
    user = AppUser(
        email=email,
        password_hash=security.get_password_hash("password"),
        name="Test Admin",
        role=Role.ADMIN,
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def create_dispatcher(
    db_session: AsyncSession, email: str = "dispatcher@example.com"
) -> AppUser:
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


async def seed_priority_rules(db_session: AsyncSession) -> None:
    """Seed the FULL tier x category matrix.

    SLA_ENGINE.md §2 requires the mapping to be total; seeding a single pair made
    every other category fail at resolution time, which surfaced as a 500 rather
    than a test failure. Reuses the production matrix so the two cannot drift.
    """
    from scripts.seed import PRIORITY_MATRIX

    for (tier, category), priority in PRIORITY_MATRIX.items():
        db_session.add(PriorityRule(tier=tier, category=category, priority=priority))
    await db_session.commit()


async def get_auth_token(client: AsyncClient, email: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": "password"})
    return str(resp.json()["access_token"])


@pytest.mark.asyncio
@pytest.mark.db
async def test_create_ticket_atomic(client: AsyncClient, db_session: AsyncSession) -> None:
    customer = await create_customer(db_session)
    await seed_priority_rules(db_session)
    token = await get_auth_token(client, customer.email)

    resp = await client.post(
        "/api/v1/tickets",
        json={"subject": "Help!", "body": "My issue", "category": "GENERAL"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 201
    data = resp.json()
    assert data["subject"] == "Help!"
    assert data["priority"] == "LOW"
    assert data["status"] == "OPEN"

    ticket_id = uuid.UUID(data["id"])

    # Verify atomicity in DB
    ticket = (await db_session.execute(select(Ticket).where(Ticket.id == ticket_id))).scalar_one()
    assert ticket.status == TicketStatus.OPEN
    events = (
        (await db_session.execute(select(TicketEvent).where(TicketEvent.ticket_id == ticket_id)))
        .scalars()
        .all()
    )

    assert len(events) == 1
    assert events[0].type == "CREATED"
    assert events[0].actor_id == customer.id
    await db_session.rollback()


@pytest.mark.asyncio
@pytest.mark.db
async def test_transition_unauthorized(client: AsyncClient, db_session: AsyncSession) -> None:
    customer = await create_customer(db_session, "cust1@example.com")
    customer2 = await create_customer(db_session, "cust2@example.com")
    await seed_priority_rules(db_session)

    token = await get_auth_token(client, customer.email)
    resp = await client.post(
        "/api/v1/tickets",
        json={"subject": "Help!", "body": "My issue", "category": "GENERAL"},
        headers={"Authorization": f"Bearer {token}"},
    )
    ticket_id = resp.json()["id"]

    # customer2 tries to transition it
    token2 = await get_auth_token(client, customer2.email)
    resp2 = await client.post(
        f"/api/v1/tickets/{ticket_id}/transitions",
        json={"to": "IN_PROGRESS"},
        headers={"Authorization": f"Bearer {token2}"},
    )

    # 404, not 403. From P16 a customer CAN drive two transitions (T5 resume,
    # T6 reopen), so the coarse gate admits them and the denial is decided
    # object-level instead: customer2 cannot see this ticket, and INV-9 says a
    # hidden resource is 404.
    #
    # The no-leak property is unchanged and still asserted below — a ticket id
    # that does not exist returns the same 404, so the two are indistinguishable.
    assert resp2.status_code == 404

    missing = await client.post(
        f"/api/v1/tickets/{uuid.uuid4()}/transitions",
        json={"to": "IN_PROGRESS"},
        headers={"Authorization": f"Bearer {token2}"},
    )
    assert missing.status_code == 404, "denial must not reveal whether the ticket exists"

    # On a ticket they DO own, the role gate is what refuses — a customer may
    # never assert that work was done.
    own = await client.post(
        f"/api/v1/tickets/{ticket_id}/transitions",
        json={"to": "IN_PROGRESS"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert own.status_code == 403


@pytest.mark.asyncio
@pytest.mark.db
async def test_transition_illegal_state(client: AsyncClient, db_session: AsyncSession) -> None:
    customer = await create_customer(db_session, "cust3@example.com")
    admin = await create_admin(db_session, "admin3@example.com")
    await seed_priority_rules(db_session)

    token_c = await get_auth_token(client, customer.email)
    resp = await client.post(
        "/api/v1/tickets",
        json={"subject": "Help!", "body": "My issue", "category": "GENERAL"},
        headers={"Authorization": f"Bearer {token_c}"},
    )
    ticket_id = resp.json()["id"]

    token_a = await get_auth_token(client, admin.email)

    # Try illegal transition (OPEN -> RESOLVED)
    resp = await client.post(
        f"/api/v1/tickets/{ticket_id}/transitions",
        json={"to": "RESOLVED"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "STATE_CONFLICT"


@pytest.mark.asyncio
@pytest.mark.db
async def test_transition_guard_fails(client: AsyncClient, db_session: AsyncSession) -> None:
    customer = await create_customer(db_session, "cust4@example.com")
    admin = await create_admin(db_session, "admin4@example.com")
    await seed_priority_rules(db_session)

    token_c = await get_auth_token(client, customer.email)
    resp = await client.post(
        "/api/v1/tickets",
        json={"subject": "Help!", "body": "My issue", "category": "GENERAL"},
        headers={"Authorization": f"Bearer {token_c}"},
    )
    ticket_id = resp.json()["id"]

    token_a = await get_auth_token(client, admin.email)

    # Try OPEN -> IN_PROGRESS but it has no assignee.
    # We added a business rule guard for this.
    resp = await client.post(
        f"/api/v1/tickets/{ticket_id}/transitions",
        json={"to": "IN_PROGRESS"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "BUSINESS_RULE_VIOLATION"


@pytest.mark.asyncio
@pytest.mark.db
async def test_create_ticket_idempotency(client: AsyncClient, db_session: AsyncSession) -> None:
    customer = await create_customer(db_session, "idempotent@example.com")
    await seed_priority_rules(db_session)
    token = await get_auth_token(client, customer.email)

    key = str(uuid.uuid4())
    payload = {"subject": "Idempotent Ticket", "body": "My issue", "category": "GENERAL"}
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": key}

    resp1 = await client.post("/api/v1/tickets", json=payload, headers=headers)
    assert resp1.status_code == 201
    ticket_id_1 = resp1.json()["id"]

    resp2 = await client.post("/api/v1/tickets", json=payload, headers=headers)
    assert resp2.status_code == 201
    ticket_id_2 = resp2.json()["id"]

    assert ticket_id_1 == ticket_id_2
    assert resp1.json() == resp2.json()

    result = await db_session.execute(select(Ticket).where(Ticket.subject == "Idempotent Ticket"))
    assert len(result.scalars().all()) == 1
