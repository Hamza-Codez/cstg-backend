"""Assignment selection (spec07 §3).

Pure: it receives candidates and returns a choice. No I/O, no database, so the
strategies are unit-testable without a server.

`None` — "no eligible agent" — is a first-class result, not an error. Every
caller handles it the same way: leave the ticket unassigned. That is a visible,
recoverable state a dispatcher can act on; forcing an over-capacity assignment
would hide it.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Strategy(StrEnum):
    MANUAL = "MANUAL"
    ROUND_ROBIN = "ROUND_ROBIN"
    LEAST_LOADED = "LEAST_LOADED"


@dataclass(frozen=True)
class Candidate:
    """An agent automation may choose.

    Only active AGENTs who accept automation are ever built into one, so INV-16
    holds by construction: this type cannot represent anyone else.
    """

    user_id: uuid.UUID
    open_tickets: int
    #: None means no ceiling — the v1 behaviour.
    max_open_tickets: int | None
    #: From the most recent ASSIGNMENT event naming this agent. None if never
    #: assigned, which sorts first so new agents are not starved.
    last_assigned_at: datetime | None


def has_capacity(candidate: Candidate) -> bool:
    if candidate.max_open_tickets is None:
        return True
    return candidate.open_tickets < candidate.max_open_tickets


def select(candidates: Sequence[Candidate], strategy: Strategy) -> uuid.UUID | None:
    """Pick an agent, or None if nobody is eligible.

    Ties break on `user_id` rather than arbitrarily, so the function is
    deterministic and testable — the same reasoning that makes
    `priority.resolve` a total mapping.
    """
    if strategy is Strategy.MANUAL:
        # Manual dispatch never auto-selects. Returning None here is what keeps
        # the default behaviour identical to v1.
        return None

    eligible = [c for c in candidates if has_capacity(c)]
    if not eligible:
        return None

    if strategy is Strategy.ROUND_ROBIN:
        # Least recently assigned first; never-assigned sorts ahead of everyone.
        return min(
            eligible,
            key=lambda c: (
                c.last_assigned_at is not None,
                c.last_assigned_at or datetime.min,
                c.user_id,
            ),
        ).user_id

    # LEAST_LOADED: fewest open tickets, then least recently assigned.
    return min(
        eligible,
        key=lambda c: (
            c.open_tickets,
            c.last_assigned_at is not None,
            c.last_assigned_at or datetime.min,
            c.user_id,
        ),
    ).user_id
