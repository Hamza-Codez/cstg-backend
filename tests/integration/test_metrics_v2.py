"""Metrics v2 against a real database (spec09 §8).

The property this file exists for: **after the pause feature, wall-clock time is
no longer the right answer for agent performance, and pause-excluded time is no
longer the right answer for customer experience.** Reporting only one of them
would make the pause either flatter the desk or slander it. Both are reported,
and this is where they are proven to differ.

Also covered here rather than in `tests/api` because they are properties of the
SQL, not of the HTTP surface: zero-filled breakdowns, status counts that
reconcile, and boundary tickets landing in exactly one bucket.
"""

import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authorization import Principal
from app.core.unit_of_work import SqlAlchemyUnitOfWork
from app.models.enums import ActorType, Category, CustomerTier, Priority, Role, TicketStatus
from app.repositories.metrics_repo import MetricsRepository
from app.schemas.ticket import TicketCreate
from app.services.metrics_service import MetricsService
from app.services.ticket_service import TicketService
from tests.api.test_tickets import create_agent, create_customer, seed_priority_rules

S = TicketStatus


async def _metrics(session_factory: Any) -> Any:
    async with session_factory() as session:
        return await MetricsService(MetricsRepository(session)).get_overview_metrics()


async def _open(
    session_factory: Any, cust: Principal, subject: str = "S", category: Category = Category.GENERAL
) -> uuid.UUID:
    async with session_factory() as session:
        service = TicketService(SqlAlchemyUnitOfWork(session))
        async with service.uow:
            ticket = await service.create_ticket(
                cust, TicketCreate(subject=subject, body="B", category=category)
            )
            return ticket.id


async def _move(session_factory: Any, principal: Principal, tid: uuid.UUID, to: S) -> None:
    async with session_factory() as session:
        service = TicketService(SqlAlchemyUnitOfWork(session))
        async with service.uow:
            await service.transition_ticket(principal, tid, to)


async def _principals(
    db_session: AsyncSession, prefix: str
) -> tuple[Principal, Principal, uuid.UUID]:
    customer = await create_customer(db_session, f"{prefix}_c@example.com")
    agent = await create_agent(db_session, f"{prefix}_a@example.com")
    await seed_priority_rules(db_session)
    cust = Principal(id=customer.id, type=ActorType.CUSTOMER, role=Role.CUSTOMER, is_active=True)
    staff = Principal(id=agent.id, type=ActorType.USER, role=Role.AGENT, is_active=True)
    return cust, staff, agent.id


@pytest.mark.db
async def test_wall_clock_and_working_time_diverge_by_exactly_the_pause(
    db_session: AsyncSession, session_factory: Any, frozen_clock: Any
) -> None:
    """**The headline property of this phase.**

    One ticket: created, worked an hour, paused three hours waiting on the
    customer, worked one more hour, resolved. Wall clock is 5h — what the
    customer experienced. Working time is 2h — what the desk actually spent.
    A dashboard reporting only the first would make the pause feature look like
    a regression in handling time.
    """
    cust, staff, agent_id = await _principals(db_session, "pw")
    tid = await _open(session_factory, cust)
    await db_session.execute(
        text("UPDATE ticket SET assignee_id = :a WHERE id = :t"), {"a": agent_id, "t": tid}
    )
    await db_session.commit()

    await _move(session_factory, staff, tid, S.IN_PROGRESS)
    frozen_clock.set(frozen_clock.now() + timedelta(hours=1))
    await _move(session_factory, staff, tid, S.PENDING_CUSTOMER)
    frozen_clock.set(frozen_clock.now() + timedelta(hours=3))
    await _move(session_factory, staff, tid, S.IN_PROGRESS)
    frozen_clock.set(frozen_clock.now() + timedelta(hours=1))
    await _move(session_factory, staff, tid, S.RESOLVED)

    overview = await _metrics(session_factory)

    assert overview.avg_resolution_seconds == 5 * 3600
    assert overview.avg_working_seconds == 2 * 3600
    assert overview.avg_working_seconds < overview.avg_resolution_seconds


@pytest.mark.db
async def test_sla_met_is_judged_on_sla_due_at_not_the_frozen_deadline(
    db_session: AsyncSession, session_factory: Any, frozen_clock: Any
) -> None:
    """A ticket resolved after its original deadline but within the paused-out
    window counts as MET.

    Judging on `deadline` would report a breach for a ticket the SLA policy
    says was on time — the pause would improve nothing, which is the whole
    point of having built it.
    """
    cust, staff, agent_id = await _principals(db_session, "met")
    tid = await _open(session_factory, cust)
    await db_session.execute(
        text("UPDATE ticket SET assignee_id = :a WHERE id = :t"), {"a": agent_id, "t": tid}
    )
    # A tight two-hour clock, so the pause is what decides the outcome.
    await db_session.execute(
        text(
            "UPDATE ticket SET deadline = created_at + interval '2 hours', "
            "sla_due_at = created_at + interval '2 hours' WHERE id = :t"
        ),
        {"t": tid},
    )
    await db_session.commit()

    await _move(session_factory, staff, tid, S.IN_PROGRESS)
    await _move(session_factory, staff, tid, S.PENDING_CUSTOMER)
    frozen_clock.set(frozen_clock.now() + timedelta(hours=4))
    await _move(session_factory, staff, tid, S.IN_PROGRESS)
    frozen_clock.set(frozen_clock.now() + timedelta(hours=1))
    await _move(session_factory, staff, tid, S.RESOLVED)

    row = (
        await db_session.execute(
            text("SELECT deadline, sla_due_at, resolved_at FROM ticket WHERE id = :t"), {"t": tid}
        )
    ).one()
    assert row.resolved_at > row.deadline, "past the original deadline"
    assert row.resolved_at <= row.sla_due_at, "but inside the paused-out window"

    overview = await _metrics(session_factory)
    assert overview.sla_met_rate == 1.0


@pytest.mark.db
async def test_every_enum_member_is_present_even_with_no_tickets(
    db_session: AsyncSession, session_factory: Any
) -> None:
    """Zero-filled, so no dashboard has to branch on a missing key — and so a
    tier with no tickets renders as an empty bar rather than vanishing."""
    overview = await _metrics(session_factory)

    assert set(overview.by_priority) == set(Priority)
    assert set(overview.by_tier) == set(CustomerTier)
    assert set(overview.by_category) == set(Category)
    assert all(group.open == 0 for group in overview.by_tier.values())


@pytest.mark.db
async def test_the_status_counts_reconcile_with_the_total(
    db_session: AsyncSession, session_factory: Any
) -> None:
    """**Including PENDING_CUSTOMER.** After spec05 the four v1 statuses no
    longer sum to the total, and a dashboard whose numbers do not add up is one
    nobody trusts."""
    cust, staff, agent_id = await _principals(db_session, "sum")

    ids = [await _open(session_factory, cust, f"T{n}") for n in range(4)]
    await db_session.execute(
        text("UPDATE ticket SET assignee_id = :a WHERE id = ANY(:ids)"),
        {"a": agent_id, "ids": ids},
    )
    await db_session.commit()

    await _move(session_factory, staff, ids[1], S.IN_PROGRESS)
    await _move(session_factory, staff, ids[2], S.IN_PROGRESS)
    await _move(session_factory, staff, ids[2], S.PENDING_CUSTOMER)
    await _move(session_factory, staff, ids[3], S.IN_PROGRESS)
    await _move(session_factory, staff, ids[3], S.RESOLVED)

    overview = await _metrics(session_factory)
    counted = (
        overview.open
        + overview.in_progress
        + overview.pending_customer
        + overview.resolved
        + overview.closed
    )
    assert counted == 4
    assert overview.pending_customer == 1


@pytest.mark.db
async def test_breakdowns_agree_with_the_totals_they_are_cut_from(
    db_session: AsyncSession, session_factory: Any
) -> None:
    """Three independent GROUP BYs over the same rows. If one gains a join that
    duplicates rows, its column stops matching the others."""
    cust, _staff, _agent_id = await _principals(db_session, "cut")
    await _open(session_factory, cust, "A", Category.GENERAL)
    await _open(session_factory, cust, "B", Category.BILLING)
    await _open(session_factory, cust, "C", Category.OUTAGE)

    overview = await _metrics(session_factory)
    for name, groups in (
        ("priority", overview.by_priority),
        ("tier", overview.by_tier),
        ("category", overview.by_category),
    ):
        assert sum(g.open for g in groups.values()) == overview.open == 3, name


@pytest.mark.db
async def test_a_boundary_ticket_lands_in_exactly_one_bucket(
    db_session: AsyncSession, session_factory: Any
) -> None:
    """Buckets are half-open, so a ticket created exactly on a boundary is
    counted once, not zero times and not twice."""
    cust, _staff, _agent_id = await _principals(db_session, "bnd")
    tid = await _open(session_factory, cust)

    await db_session.execute(
        text("UPDATE ticket SET created_at = date_trunc('day', now()) WHERE id = :t"), {"t": tid}
    )
    await db_session.commit()

    async with session_factory() as session:
        repo = MetricsRepository(session)
        start = (
            await session.execute(text("SELECT date_trunc('day', now()) - interval '3 days'"))
        ).scalar_one()
        end = (
            await session.execute(text("SELECT date_trunc('day', now()) + interval '3 days'"))
        ).scalar_one()
        points = await repo.timeseries("created", "day", start, end)

    assert sum(value for _at, value in points) == 1.0
    assert len([v for _at, v in points if v == 0.0]) >= 5, "quiet days present as zeros"
