"""Versioned SLA policy against a real database (spec06 §9).

The one thing this feature must never do is move an existing ticket's terms.
Everything else here is in service of proving that.
"""

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authorization import Principal
from app.core.unit_of_work import SqlAlchemyUnitOfWork
from app.domain.errors import BusinessRuleViolation, StateConflict
from app.models.enums import ActorType, Category, Priority, Role
from app.models.sla_policy import SlaPolicyVersion
from app.models.ticket import Ticket
from app.schemas.ticket import TicketCreate
from app.services.sla_policy_service import SlaPolicyService
from app.services.ticket_service import TicketService
from tests.api.test_tickets import create_admin, create_customer, seed_priority_rules

HALVED = {
    Priority.CRITICAL: 3600,
    Priority.HIGH: 4 * 3600,
    Priority.MEDIUM: 12 * 3600,
    Priority.LOW: 36 * 3600,
}


async def _admin(db_session: AsyncSession, email: str) -> Principal:
    user = await create_admin(db_session, email)
    return Principal(id=user.id, type=ActorType.USER, role=Role.ADMIN, is_active=True)


async def _create_ticket(db_session: AsyncSession, factory: Any, email: str) -> uuid.UUID:
    customer = await create_customer(db_session, email)
    await seed_priority_rules(db_session)
    principal = Principal(
        id=customer.id, type=ActorType.CUSTOMER, role=Role.CUSTOMER, is_active=True
    )
    async with factory() as session:
        service = TicketService(SqlAlchemyUnitOfWork(session))
        async with service.uow:
            ticket = await service.create_ticket(
                principal, TicketCreate(subject="S", body="B", category=Category.OUTAGE)
            )
            return ticket.id


async def _get(db_session: AsyncSession, tid: uuid.UUID) -> Ticket:
    db_session.expire_all()
    return (await db_session.execute(select(Ticket).where(Ticket.id == tid))).scalar_one()


async def _activate(factory: Any, principal: Principal, durations: dict, note: str = "") -> None:
    async with factory() as session:
        service = SlaPolicyService(SqlAlchemyUnitOfWork(session))
        async with service.uow:
            await service.activate(principal, durations, note=note)


@pytest.mark.db
async def test_activating_a_policy_does_not_touch_existing_tickets(
    db_session: AsyncSession, session_factory: Any
) -> None:
    """**The load-bearing test** (INV-1, INV-2, INV-15, spec06 §9).

    Halve every duration and assert the existing ticket is byte-identical.
    A ticket keeps the terms it was created under; if this ever fails, changing
    configuration has retroactively rewritten commitments.
    """
    tid = await _create_ticket(db_session, session_factory, "policy_frozen@example.com")
    before = await _get(db_session, tid)
    original = (
        before.deadline,
        before.sla_due_at,
        before.sla_policy_version_id,
        before.priority,
    )

    admin = await _admin(db_session, "policy_admin1@example.com")
    await _activate(session_factory, admin, HALVED, "halve everything")

    after = await _get(db_session, tid)
    assert (
        after.deadline,
        after.sla_due_at,
        after.sla_policy_version_id,
        after.priority,
    ) == original


@pytest.mark.db
async def test_a_ticket_created_after_activation_uses_the_new_terms(
    db_session: AsyncSession, session_factory: Any
) -> None:
    admin = await _admin(db_session, "policy_admin2@example.com")
    await _activate(session_factory, admin, HALVED, "halve everything")

    tid = await _create_ticket(db_session, session_factory, "policy_new@example.com")
    ticket = await _get(db_session, tid)

    # ENTERPRISE x OUTAGE is CRITICAL in the seeded matrix; the customer is FREE,
    # so this is whatever the matrix says — assert against the policy rather than
    # hard-coding a priority.
    expected = timedelta(seconds=HALVED[ticket.priority])
    assert ticket.deadline - ticket.created_at == expected
    assert ticket.sla_due_at == ticket.deadline

    active = (
        await db_session.execute(
            select(SlaPolicyVersion).where(SlaPolicyVersion.superseded_at.is_(None))
        )
    ).scalar_one()
    assert ticket.sla_policy_version_id == active.id


@pytest.mark.db
async def test_old_version_survives_to_explain_old_tickets(
    db_session: AsyncSession, session_factory: Any
) -> None:
    """Versions are never deleted — that is what makes a frozen deadline legible."""
    tid = await _create_ticket(db_session, session_factory, "policy_history@example.com")
    original_version = (await _get(db_session, tid)).sla_policy_version_id

    admin = await _admin(db_session, "policy_admin3@example.com")
    await _activate(session_factory, admin, HALVED, "halve everything")

    still_there = (
        await db_session.execute(
            select(SlaPolicyVersion).where(SlaPolicyVersion.id == original_version)
        )
    ).scalar_one()
    assert still_there.superseded_at is not None, "the old version is stamped, not removed"

    entries = (
        await db_session.execute(
            text("SELECT count(*) FROM sla_policy_entry WHERE version_id = :vid"),
            {"vid": original_version},
        )
    ).scalar_one()
    assert entries == 4, "its durations survive too, or it cannot explain anything"


@pytest.mark.db
async def test_exactly_one_version_is_active_after_activation(
    db_session: AsyncSession, session_factory: Any
) -> None:
    admin = await _admin(db_session, "policy_admin4@example.com")
    await _activate(session_factory, admin, HALVED, "first")
    await _activate(session_factory, admin, dict(HALVED), "second")

    active = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM sla_policy_version "
                "WHERE activated_at IS NOT NULL AND superseded_at IS NULL"
            )
        )
    ).scalar_one()
    assert active == 1


@pytest.mark.db
async def test_concurrent_activations_leave_one_winner(
    db_session: AsyncSession, session_factory: Any
) -> None:
    """The partial unique index arbitrates, not a lock (spec06 §6)."""
    admin = await _admin(db_session, "policy_admin5@example.com")

    outcomes = await asyncio.gather(
        _activate(session_factory, admin, HALVED, "racer A"),
        _activate(session_factory, admin, dict(HALVED), "racer B"),
        return_exceptions=True,
    )
    failures = [o for o in outcomes if isinstance(o, Exception)]
    assert len(failures) == 1, f"exactly one should lose, got {outcomes}"
    assert isinstance(failures[0], StateConflict)

    active = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM sla_policy_version "
                "WHERE activated_at IS NOT NULL AND superseded_at IS NULL"
            )
        )
    ).scalar_one()
    assert active == 1


@pytest.mark.db
async def test_a_partial_policy_is_refused(
    db_session: AsyncSession, session_factory: Any
) -> None:
    """Totality, the same rule the priority matrix follows (spec06 §4)."""
    admin = await _admin(db_session, "policy_admin6@example.com")
    partial = {Priority.CRITICAL: 3600, Priority.HIGH: 7200}

    with pytest.raises(BusinessRuleViolation, match="all four priorities"):
        await _activate(session_factory, admin, partial)


@pytest.mark.parametrize("seconds", [0, -1, 91 * 24 * 3600], ids=["zero", "negative", "over-cap"])
@pytest.mark.db
async def test_out_of_range_durations_are_refused(
    db_session: AsyncSession, session_factory: Any, seconds: int
) -> None:
    admin = await _admin(db_session, f"policy_range{seconds}@example.com")
    durations = dict(HALVED)
    durations[Priority.CRITICAL] = seconds

    with pytest.raises(BusinessRuleViolation):
        await _activate(session_factory, admin, durations)


@pytest.mark.db
async def test_inv15_no_write_path_changes_a_ticket_s_pinned_version(
    db_session: AsyncSession, session_factory: Any
) -> None:
    """INV-15 by construction: no repository UPDATE includes the column."""
    import inspect

    from app.repositories import ticket_repo

    source = inspect.getsource(ticket_repo)
    updates = source.split("update(Ticket)")[1:]
    for block in updates:
        body = block.split("stmt")[0]
        assert "sla_policy_version_id" not in body, (
            "a guarded UPDATE writes sla_policy_version_id — INV-15 says it never changes"
        )
