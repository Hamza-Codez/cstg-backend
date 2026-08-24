"""Lifecycle v2 pure logic (spec05 §11).

The pause arithmetic and the six-row transition table, with no database.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.domain import sla
from app.domain.state_machine import (
    is_legal_transition,
    is_reopen,
    is_role_authorized,
    pauses_clock,
    resumes_clock,
)
from app.models.enums import Priority, Role, TicketStatus

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
S = TicketStatus


# ── Pause arithmetic ─────────────────────────────────────────────────────────


def test_due_at_is_the_definition_of_inv13() -> None:
    deadline = NOW + timedelta(hours=2)
    assert sla.due_at(deadline, 0) == deadline
    assert sla.due_at(deadline, 3600) == deadline + timedelta(hours=1)


def test_accrue_pause_adds_elapsed_time() -> None:
    paused_at = NOW
    assert sla.accrue_pause(paused_at, NOW + timedelta(hours=3), 0) == 3 * 3600


def test_accrue_pause_accumulates_across_several_pauses() -> None:
    """Pause, resume, pause, resume — the total is the sum, not the last one."""
    first = sla.accrue_pause(NOW, NOW + timedelta(hours=1), 0)
    second = sla.accrue_pause(NOW + timedelta(hours=5), NOW + timedelta(hours=7), first)
    assert first == 3600
    assert second == 3 * 3600


def test_accrue_pause_never_credits_time_back() -> None:
    # A clock skew that made `now` earlier than `paused_at` would otherwise pull
    # the deadline forward — the one direction a pause must never move it.
    assert sla.accrue_pause(NOW, NOW - timedelta(hours=1), 600) == 600


def test_pause_then_resume_moves_the_due_time_by_exactly_the_pause() -> None:
    deadline = NOW + timedelta(hours=2)
    paused = sla.accrue_pause(NOW, NOW + timedelta(hours=3), 0)
    assert sla.due_at(deadline, paused) == deadline + timedelta(hours=3)


# ── Breach detection ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", [S.RESOLVED, S.CLOSED, S.PENDING_CUSTOMER])
def test_non_breaching_statuses_never_breach(status: TicketStatus) -> None:
    """A paused ticket is not late however far past its stale due time it runs.

    `sla_due_at` is stale by design while paused (spec05 §5), so this guard is
    what stops the staleness being read as lateness.
    """
    long_past = NOW - timedelta(days=365)
    assert sla.is_breach(NOW, long_past, status, None) is False


@pytest.mark.parametrize("status", [S.OPEN, S.IN_PROGRESS])
def test_active_statuses_breach_once_past_due(status: TicketStatus) -> None:
    assert sla.is_breach(NOW, NOW - timedelta(seconds=1), status, None) is True
    assert sla.is_breach(NOW, NOW + timedelta(seconds=1), status, None) is False


def test_exactly_at_due_is_not_yet_a_breach() -> None:
    # SLA_ENGINE.md §1: the comparison is `now > due`, strictly after.
    assert sla.is_breach(NOW, NOW, S.IN_PROGRESS, None) is False


def test_sla_met_is_judged_against_the_effective_due_time() -> None:
    deadline = NOW + timedelta(hours=2)
    due = sla.due_at(deadline, 3600)
    late_by_v1_rules = deadline + timedelta(minutes=30)

    # Resolved after the original deadline but inside the paused-adjusted one:
    # the desk gets the waiting time back.
    assert sla.is_sla_met(late_by_v1_rules, due) is True
    assert sla.is_sla_met(late_by_v1_rules, deadline) is False


def test_sla_met_at_exactly_the_boundary() -> None:
    due = NOW + timedelta(hours=2)
    assert sla.is_sla_met(due, due) is True
    assert sla.is_sla_met(due + timedelta(seconds=1), due) is False


def test_durations_are_unchanged_from_v1() -> None:
    assert sla.duration(Priority.CRITICAL) == timedelta(hours=2)
    assert sla.duration(Priority.LOW) == timedelta(hours=72)


# ── Transition table ─────────────────────────────────────────────────────────

LEGAL = {
    (S.OPEN, S.IN_PROGRESS),
    (S.IN_PROGRESS, S.RESOLVED),
    (S.RESOLVED, S.CLOSED),
    (S.IN_PROGRESS, S.PENDING_CUSTOMER),
    (S.PENDING_CUSTOMER, S.IN_PROGRESS),
    (S.RESOLVED, S.IN_PROGRESS),
}


@pytest.mark.parametrize("frm", list(S))
@pytest.mark.parametrize("to", list(S))
def test_only_the_six_table_rows_are_legal(frm: TicketStatus, to: TicketStatus) -> None:
    """INV-3 over the full 5x5 matrix — including that CLOSED is terminal."""
    assert is_legal_transition(frm, to) is ((frm, to) in LEGAL)


@pytest.mark.parametrize(
    ("frm", "to", "role", "allowed"),
    [
        # T1 start / T2 resolve — staff only, and not the dispatcher.
        (S.OPEN, S.IN_PROGRESS, Role.AGENT, True),
        (S.OPEN, S.IN_PROGRESS, Role.ADMIN, True),
        (S.OPEN, S.IN_PROGRESS, Role.DISPATCHER, False),
        (S.OPEN, S.IN_PROGRESS, Role.CUSTOMER, False),
        (S.IN_PROGRESS, S.RESOLVED, Role.AGENT, True),
        (S.IN_PROGRESS, S.RESOLVED, Role.CUSTOMER, False),
        # T3 close — dispatcher may, customer may not.
        (S.RESOLVED, S.CLOSED, Role.DISPATCHER, True),
        (S.RESOLVED, S.CLOSED, Role.CUSTOMER, False),
        # T4 pause — the assigned agent's call, not the customer's.
        (S.IN_PROGRESS, S.PENDING_CUSTOMER, Role.AGENT, True),
        (S.IN_PROGRESS, S.PENDING_CUSTOMER, Role.CUSTOMER, False),
        (S.IN_PROGRESS, S.PENDING_CUSTOMER, Role.DISPATCHER, False),
        # T5 resume — the customer answers the question addressed to them.
        (S.PENDING_CUSTOMER, S.IN_PROGRESS, Role.CUSTOMER, True),
        (S.PENDING_CUSTOMER, S.IN_PROGRESS, Role.AGENT, True),
        (S.PENDING_CUSTOMER, S.IN_PROGRESS, Role.DISPATCHER, False),
        # T6 reopen — "we think this is fixed" is also theirs to answer.
        (S.RESOLVED, S.IN_PROGRESS, Role.CUSTOMER, True),
        (S.RESOLVED, S.IN_PROGRESS, Role.DISPATCHER, True),
    ],
)
def test_role_authorization_per_transition(
    frm: TicketStatus, to: TicketStatus, role: Role, allowed: bool
) -> None:
    assert is_role_authorized(role, frm, to) is allowed


def test_customer_still_cannot_drive_the_work_assertions() -> None:
    """The v1 rule, narrowed rather than abandoned (spec05 §4).

    A customer may answer a question addressed to them; they may never assert
    that work was done.
    """
    for frm, to in ((S.OPEN, S.IN_PROGRESS), (S.IN_PROGRESS, S.RESOLVED), (S.RESOLVED, S.CLOSED)):
        assert is_role_authorized(Role.CUSTOMER, frm, to) is False


def test_illegal_pairs_authorize_nobody() -> None:
    assert is_role_authorized(Role.ADMIN, S.CLOSED, S.OPEN) is False


def test_transition_predicates() -> None:
    assert is_reopen(S.RESOLVED, S.IN_PROGRESS) is True
    assert is_reopen(S.OPEN, S.IN_PROGRESS) is False
    assert pauses_clock(S.PENDING_CUSTOMER) is True
    assert pauses_clock(S.RESOLVED) is False
    assert resumes_clock(S.PENDING_CUSTOMER) is True
    assert resumes_clock(S.IN_PROGRESS) is False
