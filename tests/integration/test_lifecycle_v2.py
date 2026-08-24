"""Lifecycle v2 against a real database (spec05 §11).

The three things this phase must not get wrong:
  * INV-13 — sla_due_at == deadline + sla_paused_seconds, after every transition
  * INV-14 — paused iff PENDING_CUSTOMER, enforced by the database
  * INV-6/INV-7 — a paused ticket never escalates, and a reopened one never
    breaches twice
"""

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authorization import Principal
from app.core.unit_of_work import SqlAlchemyUnitOfWork
from app.domain.errors import StateConflict
from app.models.enums import ActorType, Category, EventType, Role, TicketStatus
from app.models.ticket import Ticket
from app.models.ticket_event import TicketEvent
from app.schemas.ticket import TicketCreate
from app.services.sla_service import SLAService
from app.services.ticket_service import TicketService
from tests.api.test_tickets import create_agent, create_customer, seed_priority_rules

S = TicketStatus


async def _ticket_in_progress(
    db_session: AsyncSession, session_factory: Any, prefix: str
) -> tuple[Principal, Principal, uuid.UUID]:
    """A ticket assigned to an agent and started, plus both principals."""
    customer = await create_customer(db_session, f"{prefix}_c@example.com")
    agent = await create_agent(db_session, f"{prefix}_a@example.com")
    await seed_priority_rules(db_session)

    cust = Principal(id=customer.id, type=ActorType.CUSTOMER, role=Role.CUSTOMER, is_active=True)
    staff = Principal(id=agent.id, type=ActorType.USER, role=Role.AGENT, is_active=True)

    async with session_factory() as session:
        service = TicketService(SqlAlchemyUnitOfWork(session))
        async with service.uow:
            ticket = await service.create_ticket(
                cust, TicketCreate(subject="S", body="B", category=Category.GENERAL)
            )
            ticket_id = ticket.id

    await db_session.execute(
        text("UPDATE ticket SET assignee_id = :aid WHERE id = :tid"),
        {"aid": agent.id, "tid": ticket_id},
    )
    await db_session.commit()

    async with session_factory() as session:
        service = TicketService(SqlAlchemyUnitOfWork(session))
        async with service.uow:
            await service.transition_ticket(staff, ticket_id, S.IN_PROGRESS)

    return cust, staff, ticket_id


async def _move(session_factory: Any, principal: Principal, tid: uuid.UUID, to: S) -> None:
    async with session_factory() as session:
        service = TicketService(SqlAlchemyUnitOfWork(session))
        async with service.uow:
            await service.transition_ticket(principal, tid, to)


async def _get(db_session: AsyncSession, tid: uuid.UUID) -> Ticket:
    db_session.expire_all()
    return (await db_session.execute(select(Ticket).where(Ticket.id == tid))).scalar_one()


def _assert_inv13(ticket: Ticket) -> None:
    """Recomputed independently of the code that wrote it."""
    assert ticket.sla_due_at == ticket.deadline + timedelta(seconds=ticket.sla_paused_seconds), (
        f"INV-13 violated: due={ticket.sla_due_at} deadline={ticket.deadline} "
        f"paused={ticket.sla_paused_seconds}"
    )


@pytest.mark.db
async def test_inv13_holds_after_every_transition(
    db_session: AsyncSession, session_factory: Any, frozen_clock: Any
) -> None:
    cust, staff, tid = await _ticket_in_progress(db_session, session_factory, "inv13")
    _assert_inv13(await _get(db_session, tid))

    frozen_clock.set(frozen_clock.now() + timedelta(hours=1))
    await _move(session_factory, staff, tid, S.PENDING_CUSTOMER)
    # Paused: sla_due_at is deliberately stale, so INV-13 is asserted either side
    # of the pause rather than during it (spec05 §5).
    paused = await _get(db_session, tid)
    assert paused.sla_paused_at is not None

    frozen_clock.set(frozen_clock.now() + timedelta(hours=3))
    await _move(session_factory, staff, tid, S.IN_PROGRESS)
    resumed = await _get(db_session, tid)
    _assert_inv13(resumed)
    assert resumed.sla_paused_seconds == 3 * 3600
    assert resumed.sla_paused_at is None

    await _move(session_factory, staff, tid, S.RESOLVED)
    _assert_inv13(await _get(db_session, tid))


@pytest.mark.db
async def test_pause_moves_the_due_time_by_exactly_the_wait(
    db_session: AsyncSession, session_factory: Any, frozen_clock: Any
) -> None:
    """A CRITICAL ticket paused 3h becomes due 3h later — and not before."""
    cust, staff, tid = await _ticket_in_progress(db_session, session_factory, "pausemove")
    before = await _get(db_session, tid)
    original_due = before.sla_due_at

    await _move(session_factory, staff, tid, S.PENDING_CUSTOMER)
    frozen_clock.set(frozen_clock.now() + timedelta(hours=3))
    await _move(session_factory, staff, tid, S.IN_PROGRESS)

    after = await _get(db_session, tid)
    assert after.sla_due_at == original_due + timedelta(hours=3)
    # The promise itself never moved.
    assert after.deadline == before.deadline


@pytest.mark.db
async def test_inv14_is_enforced_by_the_database(
    db_session: AsyncSession, session_factory: Any
) -> None:
    """paused_iff_pending_customer, proven with a direct write.

    The service maintains it, but a CHECK is what makes it unbypassable.
    """
    cust, staff, tid = await _ticket_in_progress(db_session, session_factory, "inv14")

    # Paused timestamp without the paused status.
    with pytest.raises(IntegrityError):
        await db_session.execute(
            text("UPDATE ticket SET sla_paused_at = now() WHERE id = :tid"), {"tid": tid}
        )
        await db_session.commit()
    await db_session.rollback()

    # ...and the reverse: the paused status without the timestamp.
    with pytest.raises(IntegrityError):
        await db_session.execute(
            text("UPDATE ticket SET status = 'PENDING_CUSTOMER' WHERE id = :tid"), {"tid": tid}
        )
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.db
async def test_monitor_never_escalates_a_paused_ticket(
    db_session: AsyncSession, session_factory: Any, frozen_clock: Any
) -> None:
    """**The load-bearing test** (INV-7 extension, spec05 §11).

    A paused ticket long past its stale sla_due_at must be invisible to the
    monitor. This is what proves the index predicate and the candidate query
    agree — if they drift, paused tickets start escalating.
    """
    cust, staff, tid = await _ticket_in_progress(db_session, session_factory, "pausescan")
    await _move(session_factory, staff, tid, S.PENDING_CUSTOMER)

    # Far beyond any deadline the ticket could have had.
    frozen_clock.set(frozen_clock.now() + timedelta(days=30))

    async with session_factory() as session:
        escalated = await SLAService(SqlAlchemyUnitOfWork(session)).escalate_due_breaches(
            frozen_clock.now()
        )
    assert escalated == 0

    ticket = await _get(db_session, tid)
    assert ticket.sla_breached_at is None
    assert ticket.escalation_level == 0


@pytest.mark.db
async def test_reopen_keeps_the_breach_so_inv6_holds(
    db_session: AsyncSession, session_factory: Any, frozen_clock: Any
) -> None:
    """A service must not be able to erase its own failures by reopening.

    sla_breached_at stays set, so the monitor's idempotency guard still refuses
    a second escalation. reopen_count carries the reopen signal instead.
    """
    cust, staff, tid = await _ticket_in_progress(db_session, session_factory, "reopeninv6")

    # Let it breach.
    frozen_clock.set(frozen_clock.now() + timedelta(days=10))
    async with session_factory() as session:
        assert (
            await SLAService(SqlAlchemyUnitOfWork(session)).escalate_due_breaches(
                frozen_clock.now()
            )
            == 1
        )
    breached = await _get(db_session, tid)
    assert breached.sla_breached_at is not None
    assert breached.escalation_level == 1

    await _move(session_factory, staff, tid, S.RESOLVED)
    await _move(session_factory, staff, tid, S.IN_PROGRESS)  # T6

    reopened = await _get(db_session, tid)
    assert reopened.reopen_count == 1
    assert reopened.resolved_at is None, "resolved_requires_status forbids it on IN_PROGRESS"
    assert reopened.sla_breached_at == breached.sla_breached_at, "breach must survive reopen"
    assert reopened.escalation_level == 1, "escalation_level must never go down"

    # A second scan writes no second breach event (INV-6).
    async with session_factory() as session:
        assert (
            await SLAService(SqlAlchemyUnitOfWork(session)).escalate_due_breaches(
                frozen_clock.now()
            )
            == 0
        )
    events = (
        (
            await db_session.execute(
                select(TicketEvent).where(
                    TicketEvent.ticket_id == tid, TicketEvent.type == EventType.SLA_BREACH
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1


@pytest.mark.db
async def test_reopen_before_due_can_still_breach_for_the_first_time(
    db_session: AsyncSession, session_factory: Any, frozen_clock: Any
) -> None:
    """A ticket whose guard was never tripped behaves entirely normally."""
    cust, staff, tid = await _ticket_in_progress(db_session, session_factory, "reopenfresh")

    await _move(session_factory, staff, tid, S.RESOLVED)
    await _move(session_factory, staff, tid, S.IN_PROGRESS)
    assert (await _get(db_session, tid)).sla_breached_at is None

    frozen_clock.set(frozen_clock.now() + timedelta(days=10))
    async with session_factory() as session:
        assert (
            await SLAService(SqlAlchemyUnitOfWork(session)).escalate_due_breaches(
                frozen_clock.now()
            )
            == 1
        )
    assert (await _get(db_session, tid)).escalation_level == 1


@pytest.mark.db
async def test_concurrent_resume_accrues_the_pause_exactly_once(
    db_session: AsyncSession, session_factory: Any, frozen_clock: Any
) -> None:
    """Customer auto-resume racing an agent's manual resume (spec05 §11).

    The guarded UPDATE means one wins and the other gets 409 — and crucially the
    paused time is added once, not twice.
    """
    cust, staff, tid = await _ticket_in_progress(db_session, session_factory, "resumerace")
    await _move(session_factory, staff, tid, S.PENDING_CUSTOMER)

    # Frozen so the accrued total is exact rather than "however long the test
    # took" — an inexact assertion here could not tell one accrual from two.
    frozen_clock.set(frozen_clock.now() + timedelta(hours=2))

    outcomes = await asyncio.gather(
        _move(session_factory, staff, tid, S.IN_PROGRESS),
        _move(session_factory, cust, tid, S.IN_PROGRESS),
        return_exceptions=True,
    )

    conflicts = [o for o in outcomes if isinstance(o, StateConflict)]
    assert len(conflicts) == 1, f"exactly one should lose the race, got {outcomes}"

    resumed = await _get(db_session, tid)
    assert resumed.status is S.IN_PROGRESS
    _assert_inv13(resumed)
    # Exactly one accrual. Two would read 4h and still satisfy INV-13, so this
    # is the assertion that catches a double-resume.
    assert resumed.sla_paused_seconds == 2 * 3600
    assert resumed.sla_paused_at is None


@pytest.mark.db
async def test_monitor_escalation_loses_to_a_pause(
    db_session: AsyncSession, session_factory: Any, frozen_clock: Any
) -> None:
    """A human pausing the clock first wins; the escalation becomes a no-op."""
    cust, staff, tid = await _ticket_in_progress(db_session, session_factory, "pauserace")
    frozen_clock.set(frozen_clock.now() + timedelta(days=10))

    await _move(session_factory, staff, tid, S.PENDING_CUSTOMER)

    async with session_factory() as session:
        escalated = await SLAService(SqlAlchemyUnitOfWork(session)).escalate_due_breaches(
            frozen_clock.now()
        )
    assert escalated == 0
    assert (await _get(db_session, tid)).sla_breached_at is None
