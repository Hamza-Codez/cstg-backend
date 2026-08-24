import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.models.customer import Customer
from app.models.enums import Category, CustomerTier, Priority, Role
from app.models.priority_rule import PriorityRule
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
    db_session: AsyncSession, email: str = "agent@example.com", is_active: bool = True
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


async def seed_priority_rules(db_session: AsyncSession) -> None:
    rule = PriorityRule(tier=CustomerTier.FREE, category=Category.GENERAL, priority=Priority.LOW)
    db_session.add(rule)
    await db_session.commit()


async def get_auth_token(client: AsyncClient, email: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": "password"})
    return str(resp.json()["access_token"])


@pytest.mark.asyncio
@pytest.mark.db
async def test_assign_ticket_happy_path(client: AsyncClient, db_session: AsyncSession) -> None:
    # Setup
    customer = await create_customer(db_session)
    agent = await create_agent(db_session)
    dispatcher = await create_dispatcher(db_session)
    await seed_priority_rules(db_session)

    # Customer creates a ticket
    token_c = await get_auth_token(client, customer.email)
    resp = await client.post(
        "/api/v1/tickets",
        json={"subject": "Help", "body": "Issue", "category": "GENERAL"},
        headers={"Authorization": f"Bearer {token_c}"},
    )
    ticket_id = resp.json()["id"]

    # Dispatcher assigns the ticket
    token_d = await get_auth_token(client, dispatcher.email)
    resp_assign = await client.post(
        f"/api/v1/tickets/{ticket_id}/assignment",
        json={"assignee_id": str(agent.id)},
        headers={"Authorization": f"Bearer {token_d}"},
    )

    assert resp_assign.status_code == 200

    # The T1 guard now passes because the ticket has an assignee. The transition is
    # driven by the assigned AGENT: AUTHORIZATION.md §3 gives DISPATCHER only T3,
    # so a dispatcher starting work would (correctly) be 403.
    token_a = await get_auth_token(client, agent.email)
    resp_start = await client.post(
        f"/api/v1/tickets/{ticket_id}/transitions",
        json={"to": "IN_PROGRESS"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp_start.status_code == 200
    assert resp_start.json()["status"] == "IN_PROGRESS"


@pytest.mark.asyncio
@pytest.mark.db
async def test_assign_inactive_or_wrong_role(client: AsyncClient, db_session: AsyncSession) -> None:
    customer = await create_customer(db_session)
    inactive_agent = await create_agent(db_session, "inactive@example.com", is_active=False)
    dispatcher = await create_dispatcher(db_session)
    await seed_priority_rules(db_session)

    token_c = await get_auth_token(client, customer.email)
    resp = await client.post(
        "/api/v1/tickets",
        json={"subject": "Help", "body": "Issue", "category": "GENERAL"},
        headers={"Authorization": f"Bearer {token_c}"},
    )
    ticket_id = resp.json()["id"]

    token_d = await get_auth_token(client, dispatcher.email)

    # Assign to inactive agent -> 422
    resp_assign_inactive = await client.post(
        f"/api/v1/tickets/{ticket_id}/assignment",
        json={"assignee_id": str(inactive_agent.id)},
        headers={"Authorization": f"Bearer {token_d}"},
    )
    assert resp_assign_inactive.status_code == 422
    assert resp_assign_inactive.json()["error"]["code"] == "BUSINESS_RULE_VIOLATION"

    # Assign to customer (not an agent) -> 422
    resp_assign_cust = await client.post(
        f"/api/v1/tickets/{ticket_id}/assignment",
        json={"assignee_id": str(customer.id)},
        headers={"Authorization": f"Bearer {token_d}"},
    )
    assert resp_assign_cust.status_code == 422


@pytest.mark.asyncio
@pytest.mark.db
async def test_assign_unauthorized(client: AsyncClient, db_session: AsyncSession) -> None:
    customer = await create_customer(db_session)
    agent = await create_agent(db_session)
    await seed_priority_rules(db_session)

    token_c = await get_auth_token(client, customer.email)
    resp = await client.post(
        "/api/v1/tickets",
        json={"subject": "Help", "body": "Issue", "category": "GENERAL"},
        headers={"Authorization": f"Bearer {token_c}"},
    )
    ticket_id = resp.json()["id"]

    # Customer attempts to assign
    resp_assign_c = await client.post(
        f"/api/v1/tickets/{ticket_id}/assignment",
        json={"assignee_id": str(agent.id)},
        headers={"Authorization": f"Bearer {token_c}"},
    )
    # The coarse role guard in router isn't there, wait, the service throws Forbidden
    assert resp_assign_c.status_code == 403

    # Agent attempts to assign
    token_a = await get_auth_token(client, agent.email)
    resp_assign_a = await client.post(
        f"/api/v1/tickets/{ticket_id}/assignment",
        json={"assignee_id": str(agent.id)},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp_assign_a.status_code == 403


@pytest.mark.asyncio
@pytest.mark.db
async def test_assign_terminal_state_fails(client: AsyncClient, db_session: AsyncSession) -> None:
    customer = await create_customer(db_session)
    agent = await create_agent(db_session)
    agent2 = await create_agent(db_session, "agent2@example.com")
    dispatcher = await create_dispatcher(db_session)
    await seed_priority_rules(db_session)

    token_c = await get_auth_token(client, customer.email)
    resp = await client.post(
        "/api/v1/tickets",
        json={"subject": "Help", "body": "Issue", "category": "GENERAL"},
        headers={"Authorization": f"Bearer {token_c}"},
    )
    ticket_id = resp.json()["id"]

    token_d = await get_auth_token(client, dispatcher.email)
    token_a = await get_auth_token(client, agent.email)

    # 1. Assign to agent
    await client.post(
        f"/api/v1/tickets/{ticket_id}/assignment",
        json={"assignee_id": str(agent.id)},
        headers={"Authorization": f"Bearer {token_d}"},
    )

    # 2. Transition to IN_PROGRESS then RESOLVED (using agent token!)
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

    # 3. Try to re-assign a RESOLVED ticket (should fail state guard)
    resp_reassign = await client.post(
        f"/api/v1/tickets/{ticket_id}/assignment",
        json={"assignee_id": str(agent2.id)},
        headers={"Authorization": f"Bearer {token_d}"},
    )
    assert resp_reassign.status_code == 409
    assert resp_reassign.json()["error"]["code"] == "STATE_CONFLICT"
