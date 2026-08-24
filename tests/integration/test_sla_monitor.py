import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import security
from app.core.clock import now
from app.core.unit_of_work import SqlAlchemyUnitOfWork
from app.models.customer import Customer
from app.models.enums import Category, CustomerTier, EventType, Priority, TicketStatus
from app.models.priority_rule import PriorityRule
from app.models.ticket import Ticket
from app.models.ticket_event import TicketEvent
from app.repositories.ticket_repo import TransitionWrites
from app.services.sla_service import SLAService


async def setup_test_data(db_session: AsyncSession) -> Customer:
    customer = Customer(
        email="sla@example.com",
        password_hash=security.get_password_hash("password"),
        name="Test Cust",
        tier=CustomerTier.FREE,
    )
    rule = PriorityRule(tier=CustomerTier.FREE, category=Category.GENERAL, priority=Priority.LOW)
    db_session.add(customer)
    db_session.add(rule)
    await db_session.commit()
    return customer


async def create_overdue_ticket(db_session: AsyncSession, customer_id: uuid.UUID) -> uuid.UUID:
    """Creates a ticket and manually sets its deadline to the past."""
    ticket = Ticket(
        subject="Overdue",
        body="Test body",
        category=Category.GENERAL,
        priority=Priority.LOW,
        customer_id=customer_id,
        deadline=now() - timedelta(hours=1),
    )
    db_session.add(ticket)
    await db_session.commit()
    return ticket.id


@pytest.mark.asyncio
@pytest.mark.db
async def test_sla_escalation_idempotency(db_session: AsyncSession) -> None:
    customer = await setup_test_data(db_session)
    ticket_id = await create_overdue_ticket(db_session, customer.id)

    uow = SqlAlchemyUnitOfWork(db_session)
    sla_service = SLAService(uow)

    # 1. First scan should escalate the ticket
    escalated = await sla_service.escalate_due_breaches(now())
    assert escalated == 1

    ticket = (await db_session.execute(select(Ticket).where(Ticket.id == ticket_id))).scalar_one()
    assert ticket.escalation_level == 1
    assert ticket.sla_breached_at is not None

    events = (
        (await db_session.execute(select(TicketEvent).where(TicketEvent.ticket_id == ticket_id)))
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].type == EventType.SLA_BREACH

    # 2. Second scan should be a no-op (idempotent)
    escalated_again = await sla_service.escalate_due_breaches(now())
    assert escalated_again == 0

    ticket_again = (
        await db_session.execute(select(Ticket).where(Ticket.id == ticket_id))
    ).scalar_one()
    assert ticket_again.escalation_level == 1  # unchanged

    await db_session.commit()


@pytest.mark.asyncio
@pytest.mark.db
async def test_sla_never_escalates_terminal(db_session: AsyncSession) -> None:
    customer = await setup_test_data(db_session)
    ticket = Ticket(
        subject="Terminal",
        body="Test",
        category=Category.GENERAL,
        priority=Priority.LOW,
        customer_id=customer.id,
        status=TicketStatus.RESOLVED,
        deadline=now() - timedelta(hours=1),  # overdue
        resolved_at=now() - timedelta(minutes=30),
    )
    db_session.add(ticket)
    await db_session.commit()

    uow = SqlAlchemyUnitOfWork(db_session)
    sla_service = SLAService(uow)

    escalated = await sla_service.escalate_due_breaches(now())
    assert escalated == 0
    await db_session.commit()


@pytest.mark.asyncio
@pytest.mark.db
async def test_sla_concurrency_race_with_resolve(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """
    Tests the concurrency guard (INV-7) to ensure an SLA breach doesn't double-write
    if a human resolves the ticket exactly when the monitor runs.
    """
    customer = await setup_test_data(db_session)
    ticket_id = await create_overdue_ticket(db_session, customer.id)

    # We will simulate the SLA monitor running concurrently with a TicketService.resolve.

    async def simulate_human_resolution() -> None:
        # A human resolves the ticket in their own session
        async with session_factory() as s:
            uow = SqlAlchemyUnitOfWork(s)
            ticket_repo = uow.tickets

            # Use the guarded conditional update that TicketService uses (P3/T2 guard)
            async with uow:
                await ticket_repo.transition_if(
                    ticket_id,
                    TicketStatus.OPEN,
                    TicketStatus.RESOLVED,
                    writes=TransitionWrites(resolved_at=now()),
                )

    async def simulate_sla_monitor() -> None:
        # SLA monitor runs in its own session
        async with session_factory() as s:
            uow = SqlAlchemyUnitOfWork(s)

            async with uow:
                # We do candidate select
                candidates = await uow.tickets.get_breach_candidates(now())

                # Yield control to the human resolution task intentionally here
                await asyncio.sleep(0.1)

                # Now try to escalate the candidate
                if candidates:
                    await uow.tickets.escalate_if(candidates[0].id, now())

    # Run them concurrently
    await asyncio.gather(simulate_sla_monitor(), simulate_human_resolution())

    # Verify that the ticket is RESOLVED and did NOT get escalated
    ticket = (await db_session.execute(select(Ticket).where(Ticket.id == ticket_id))).scalar_one()

    assert ticket.status == TicketStatus.RESOLVED
    assert ticket.escalation_level == 0
    assert ticket.sla_breached_at is None
    await db_session.commit()
