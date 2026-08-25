"""Assignment automation against a real database (spec07 §8).

The two things that must hold: a claim race has exactly one winner, and
**every** assignment path — manual, claimed, automatic — targets an active AGENT
(INV-16).
"""

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authorization import Principal
from app.core.unit_of_work import SqlAlchemyUnitOfWork
from app.domain.errors import BusinessRuleViolation, StateConflict
from app.models.assignment_config import AssignmentConfig
from app.models.enums import ActorType, Category, EventType, Role
from app.models.ticket import Ticket
from app.models.ticket_event import TicketEvent
from app.schemas.ticket import TicketCreate
from app.services.assignment_service import AssignmentService
from app.services.ticket_service import TicketService
from tests.api.test_tickets import create_agent, create_customer, seed_priority_rules


async def _customer(db_session: AsyncSession, email: str) -> Principal:
    customer = await create_customer(db_session, email)
    await seed_priority_rules(db_session)
    return Principal(id=customer.id, type=ActorType.CUSTOMER, role=Role.CUSTOMER, is_active=True)


async def _agent(db_session: AsyncSession, email: str, **kwargs: Any) -> Principal:
    agent = await create_agent(db_session, email)
    if kwargs:
        await db_session.execute(
            text(
                "UPDATE app_user SET "
                + ", ".join(f"{k} = :{k}" for k in kwargs)
                + " WHERE id = :uid"
            ),
            {**kwargs, "uid": agent.id},
        )
        await db_session.commit()
    return Principal(id=agent.id, type=ActorType.USER, role=Role.AGENT, is_active=True)


async def _new_ticket(factory: Any, customer: Principal) -> uuid.UUID:
    async with factory() as session:
        service = TicketService(SqlAlchemyUnitOfWork(session))
        async with service.uow:
            ticket = await service.create_ticket(
                customer, TicketCreate(subject="S", body="B", category=Category.GENERAL)
            )
            return ticket.id


async def _claim(factory: Any, principal: Principal, tid: uuid.UUID) -> None:
    async with factory() as session:
        service = AssignmentService(SqlAlchemyUnitOfWork(session))
        async with service.uow:
            await service.claim_ticket(principal, tid)


async def _set_config(db_session: AsyncSession, strategy: str, auto: bool) -> None:
    await db_session.execute(
        text("UPDATE assignment_config SET strategy = :s, auto_assign_on_create = :a"),
        {"s": strategy, "a": auto},
    )
    await db_session.commit()


async def _get(db_session: AsyncSession, tid: uuid.UUID) -> Ticket:
    db_session.expire_all()
    return (await db_session.execute(select(Ticket).where(Ticket.id == tid))).scalar_one()


@pytest.mark.db
async def test_two_agents_claiming_the_same_ticket_have_one_winner(
    db_session: AsyncSession, session_factory: Any
) -> None:
    """**The claim race.** `assignee_id IS NULL` is the guard.

    Two agents scanning the same queue will race; one wins, the other gets a
    409, and exactly one ASSIGNMENT event is written.
    """
    customer = await _customer(db_session, "claimrace_c@example.com")
    first = await _agent(db_session, "claimrace_a1@example.com")
    second = await _agent(db_session, "claimrace_a2@example.com")
    tid = await _new_ticket(session_factory, customer)

    outcomes = await asyncio.gather(
        _claim(session_factory, first, tid),
        _claim(session_factory, second, tid),
        return_exceptions=True,
    )
    conflicts = [o for o in outcomes if isinstance(o, StateConflict)]
    assert len(conflicts) == 1, f"exactly one should lose, got {outcomes}"

    ticket = await _get(db_session, tid)
    assert ticket.assignee_id in {first.id, second.id}

    events = (
        (
            await db_session.execute(
                select(TicketEvent).where(
                    TicketEvent.ticket_id == tid, TicketEvent.type == EventType.ASSIGNMENT
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1


@pytest.mark.db
async def test_claiming_an_already_assigned_ticket_is_refused(
    db_session: AsyncSession, session_factory: Any
) -> None:
    """Claiming is taking, not reassigning — that is a dispatcher action."""
    customer = await _customer(db_session, "claimtaken_c@example.com")
    owner = await _agent(db_session, "claimtaken_a1@example.com")
    other = await _agent(db_session, "claimtaken_a2@example.com")
    tid = await _new_ticket(session_factory, customer)

    await _claim(session_factory, owner, tid)
    with pytest.raises(StateConflict):
        await _claim(session_factory, other, tid)

    assert (await _get(db_session, tid)).assignee_id == owner.id


@pytest.mark.db
async def test_claiming_at_capacity_is_refused_with_a_reason(
    db_session: AsyncSession, session_factory: Any
) -> None:
    """An agent deliberately taking work deserves to know why it was refused."""
    customer = await _customer(db_session, "claimcap_c@example.com")
    agent = await _agent(db_session, "claimcap_a@example.com", max_open_tickets=1)

    first = await _new_ticket(session_factory, customer)
    await _claim(session_factory, agent, first)

    second = await _new_ticket(session_factory, customer)
    with pytest.raises(BusinessRuleViolation, match="ticket limit"):
        await _claim(session_factory, agent, second)

    assert (await _get(db_session, second)).assignee_id is None


@pytest.mark.db
async def test_round_robin_distributes_across_agents(
    db_session: AsyncSession, session_factory: Any
) -> None:
    """Driven by real ASSIGNMENT events, not a counter column (spec07 §3)."""
    customer = await _customer(db_session, "rr_c@example.com")
    agents = [await _agent(db_session, f"rr_a{i}@example.com") for i in range(3)]
    await _set_config(db_session, "ROUND_ROBIN", True)

    assigned = []
    for _ in range(6):
        tid = await _new_ticket(session_factory, customer)
        assigned.append((await _get(db_session, tid)).assignee_id)

    # Every agent used, and no agent taking more than one extra.
    counts = {a.id: assigned.count(a.id) for a in agents}
    assert all(count == 2 for count in counts.values()), counts


@pytest.mark.db
async def test_auto_assign_commits_with_the_ticket(
    db_session: AsyncSession, session_factory: Any
) -> None:
    """Same transaction, so a ticket is never briefly visible unassigned."""
    customer = await _customer(db_session, "auto_c@example.com")
    agent = await _agent(db_session, "auto_a@example.com")
    await _set_config(db_session, "LEAST_LOADED", True)

    tid = await _new_ticket(session_factory, customer)
    ticket = await _get(db_session, tid)
    assert ticket.assignee_id == agent.id

    events = (
        (
            await db_session.execute(
                select(TicketEvent).where(
                    TicketEvent.ticket_id == tid, TicketEvent.type == EventType.ASSIGNMENT
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    # SYSTEM actor, like the SLA monitor — and system_actor_has_no_id requires
    # actor_id to be NULL.
    assert events[0].actor_type is ActorType.SYSTEM
    assert events[0].actor_id is None
    assert events[0].detail["automatic"] is True


@pytest.mark.db
async def test_no_eligible_agent_leaves_the_ticket_unassigned_without_failing(
    db_session: AsyncSession, session_factory: Any
) -> None:
    """A normal outcome, not an error: creation still succeeds."""
    customer = await _customer(db_session, "noagent_c@example.com")
    await _agent(db_session, "noagent_a@example.com", max_open_tickets=1)
    await _set_config(db_session, "ROUND_ROBIN", True)

    first = await _new_ticket(session_factory, customer)
    assert (await _get(db_session, first)).assignee_id is not None

    # The only agent is now full.
    second = await _new_ticket(session_factory, customer)
    ticket = await _get(db_session, second)
    assert ticket.assignee_id is None

    events = (
        (
            await db_session.execute(
                select(TicketEvent).where(
                    TicketEvent.ticket_id == second, TicketEvent.type == EventType.ASSIGNMENT
                )
            )
        )
        .scalars()
        .all()
    )
    assert events == [], "no agent chosen means no ASSIGNMENT event"


@pytest.mark.db
async def test_inv16_automation_never_picks_an_ineligible_user(
    db_session: AsyncSession, session_factory: Any
) -> None:
    """INV-16: inactive agents, opted-out agents, and non-agents are never chosen."""
    customer = await _customer(db_session, "inv16_c@example.com")
    await _agent(db_session, "inv16_inactive@example.com", is_active=False)
    await _agent(db_session, "inv16_optout@example.com", accepts_auto_assignment=False)
    eligible = await _agent(db_session, "inv16_ok@example.com")
    await _set_config(db_session, "LEAST_LOADED", True)

    for _ in range(3):
        tid = await _new_ticket(session_factory, customer)
        assert (await _get(db_session, tid)).assignee_id == eligible.id


@pytest.mark.db
async def test_manual_strategy_assigns_nothing(
    db_session: AsyncSession, session_factory: Any
) -> None:
    """The shipped default. Behaviour identical to v1 until an admin opts in."""
    customer = await _customer(db_session, "manual_c@example.com")
    await _agent(db_session, "manual_a@example.com")

    config = (await db_session.execute(select(AssignmentConfig))).scalar_one()
    assert config.strategy == "MANUAL"
    assert config.auto_assign_on_create is False

    tid = await _new_ticket(session_factory, customer)
    assert (await _get(db_session, tid)).assignee_id is None


@pytest.mark.db
async def test_pending_customer_tickets_count_toward_load(
    db_session: AsyncSession, session_factory: Any
) -> None:
    """A ticket waiting on a customer still occupies its agent's slot."""
    customer = await _customer(db_session, "loadpaused_c@example.com")
    agent = await _agent(db_session, "loadpaused_a@example.com", max_open_tickets=1)

    tid = await _new_ticket(session_factory, customer)
    await _claim(session_factory, agent, tid)

    await db_session.execute(
        text(
            "UPDATE ticket SET status = 'PENDING_CUSTOMER', sla_paused_at = now() WHERE id = :tid"
        ),
        {"tid": tid},
    )
    await db_session.commit()

    second = await _new_ticket(session_factory, customer)
    with pytest.raises(BusinessRuleViolation, match="ticket limit"):
        await _claim(session_factory, agent, second)
