from datetime import datetime, timedelta

from app.models.enums import Priority, TicketStatus

_DURATION_BY_PRIORITY = {
    Priority.CRITICAL: timedelta(hours=2),
    Priority.HIGH: timedelta(hours=8),
    Priority.MEDIUM: timedelta(hours=24),
    Priority.LOW: timedelta(hours=72),
}

#: Statuses that are never breach candidates. RESOLVED and CLOSED are terminal;
#: PENDING_CUSTOMER is paused, and the desk is not the blocker (spec05 §5).
NON_BREACHING = (TicketStatus.RESOLVED, TicketStatus.CLOSED, TicketStatus.PENDING_CUSTOMER)


def duration(priority: Priority) -> timedelta:
    """Return the SLA duration for a priority."""
    return _DURATION_BY_PRIORITY[priority]


def deadline_for(created_at: datetime, priority: Priority) -> datetime:
    """Compute the immutable deadline for a ticket (INV-1, INV-2).

    Written once at creation and never recomputed — not even by a pause, which
    accrues into a separate column instead.
    """
    return created_at + duration(priority)


def due_at(deadline: datetime, paused_seconds: int) -> datetime:
    """The effective due time. **INV-13**: ``sla_due_at == deadline + paused_seconds``.

    Deliberately trivial and deliberately present: this is the single definition
    of INV-13, so the service, the migration backfill, and the tests all compute
    it the same way rather than each reimplementing the addition.
    """
    return deadline + timedelta(seconds=paused_seconds)


def accrue_pause(paused_at: datetime, now: datetime, paused_seconds: int) -> int:
    """Total paused seconds after a pause that began at ``paused_at`` ends now.

    Clamped at zero: a clock skew that made ``now`` earlier than ``paused_at``
    would otherwise *credit* time back and pull the deadline forward, which is
    the one direction a pause must never move it.
    """
    elapsed = int((now - paused_at).total_seconds())
    return paused_seconds + max(elapsed, 0)


def is_sla_met(resolved_at: datetime | None, due: datetime) -> bool:
    """Whether the SLA was met. Comparison is ``<=`` per SLA_ENGINE.md §4.

    Judged against the effective due time, so a ticket that legitimately waited
    on its customer gets that time back.
    """
    if resolved_at is None:
        return False
    return resolved_at <= due


def is_breach(
    now: datetime, due: datetime, status: TicketStatus, sla_breached_at: datetime | None
) -> bool:
    """Whether a ticket is currently breaching.

    A breach occurs if ``now > due`` and the ticket is neither terminal nor
    paused. ``sla_breached_at`` is not consulted here — the monitor's guard uses
    ``sla_breached_at IS NULL`` for idempotency (INV-6), which is a separate
    concern from whether the ticket is late.
    """
    if status in NON_BREACHING:
        return False
    return now > due
