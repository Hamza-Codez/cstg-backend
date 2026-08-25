"""Assignment selection (spec07 §8). Pure — no database."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.domain.assignment import Candidate, Strategy, has_capacity, select

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
A = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
B = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
C = uuid.UUID("00000000-0000-0000-0000-0000000000cc")


def agent(uid: uuid.UUID, open_tickets: int = 0, cap: int | None = None, last: int | None = None):
    return Candidate(
        user_id=uid,
        open_tickets=open_tickets,
        max_open_tickets=cap,
        last_assigned_at=None if last is None else NOW - timedelta(hours=last),
    )


# ── capacity ─────────────────────────────────────────────────────────────────


def test_no_ceiling_means_always_available() -> None:
    # max_open_tickets IS NULL is the v1 behaviour; existing staff are unaffected.
    assert has_capacity(agent(A, open_tickets=999, cap=None)) is True


def test_at_the_ceiling_is_full() -> None:
    assert has_capacity(agent(A, open_tickets=5, cap=5)) is False
    assert has_capacity(agent(A, open_tickets=4, cap=5)) is True


# ── MANUAL ───────────────────────────────────────────────────────────────────


def test_manual_never_selects() -> None:
    """Returning None is what keeps the default identical to v1."""
    assert select([agent(A), agent(B)], Strategy.MANUAL) is None


# ── no eligible agent ────────────────────────────────────────────────────────


@pytest.mark.parametrize("strategy", [Strategy.ROUND_ROBIN, Strategy.LEAST_LOADED])
def test_none_when_everyone_is_full(strategy: Strategy) -> None:
    """A first-class result, not an error: the ticket stays unassigned, which is
    a visible state a dispatcher can act on."""
    full = [agent(A, open_tickets=5, cap=5), agent(B, open_tickets=3, cap=3)]
    assert select(full, strategy) is None


@pytest.mark.parametrize("strategy", [Strategy.ROUND_ROBIN, Strategy.LEAST_LOADED])
def test_none_when_there_are_no_candidates(strategy: Strategy) -> None:
    assert select([], strategy) is None


# ── ROUND_ROBIN ──────────────────────────────────────────────────────────────


def test_round_robin_picks_least_recently_assigned() -> None:
    candidates = [agent(A, last=1), agent(B, last=5), agent(C, last=3)]
    assert select(candidates, Strategy.ROUND_ROBIN) == B


def test_round_robin_prefers_a_never_assigned_agent() -> None:
    # Otherwise a new agent is starved behind everyone with a history.
    candidates = [agent(A, last=10), agent(B, last=None)]
    assert select(candidates, Strategy.ROUND_ROBIN) == B


def test_round_robin_skips_agents_at_capacity() -> None:
    # B is the least recently assigned but is full, so A takes it.
    candidates = [agent(A, last=1), agent(B, open_tickets=2, cap=2, last=9)]
    assert select(candidates, Strategy.ROUND_ROBIN) == A


def test_round_robin_ignores_load_when_there_is_room() -> None:
    """Round robin takes turns; it is not least-loaded wearing a hat."""
    candidates = [agent(A, open_tickets=9, last=8), agent(B, open_tickets=0, last=1)]
    assert select(candidates, Strategy.ROUND_ROBIN) == A


# ── LEAST_LOADED ─────────────────────────────────────────────────────────────


def test_least_loaded_picks_the_smallest_queue() -> None:
    candidates = [agent(A, open_tickets=4), agent(B, open_tickets=1), agent(C, open_tickets=7)]
    assert select(candidates, Strategy.LEAST_LOADED) == B


def test_least_loaded_breaks_a_load_tie_by_least_recently_assigned() -> None:
    candidates = [agent(A, open_tickets=2, last=1), agent(B, open_tickets=2, last=6)]
    assert select(candidates, Strategy.LEAST_LOADED) == B


def test_least_loaded_skips_a_full_agent_even_with_the_smallest_queue() -> None:
    candidates = [agent(A, open_tickets=1, cap=1), agent(B, open_tickets=3)]
    assert select(candidates, Strategy.LEAST_LOADED) == B


# ── determinism ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("strategy", [Strategy.ROUND_ROBIN, Strategy.LEAST_LOADED])
def test_ties_break_deterministically_on_user_id(strategy: Strategy) -> None:
    """Identical candidates must not select arbitrarily, or the function is
    untestable and the behaviour unreproducible."""
    candidates = [agent(C, last=2), agent(A, last=2), agent(B, last=2)]
    first = select(candidates, strategy)
    assert first == A
    # Order of input must not matter.
    assert select(list(reversed(candidates)), strategy) == first
