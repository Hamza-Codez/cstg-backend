from datetime import datetime, timedelta

from app.models.enums import Priority, TicketStatus

_DURATION_BY_PRIORITY = {
    Priority.CRITICAL: timedelta(hours=2),
    Priority.HIGH: timedelta(hours=8),
    Priority.MEDIUM: timedelta(hours=24),
    Priority.LOW: timedelta(hours=72),
}


def duration(priority: Priority) -> timedelta:
    """Return the SLA duration for a priority."""
    return _DURATION_BY_PRIORITY[priority]


def deadline_for(created_at: datetime, priority: Priority) -> datetime:
    """Compute the immutable deadline for a ticket."""
    return created_at + duration(priority)


def is_sla_met(resolved_at: datetime | None, deadline: datetime) -> bool:
    """Check if the SLA was met. Comparison is <= per SLA_ENGINE.md §4."""
    if resolved_at is None:
        return False
    return resolved_at <= deadline


def is_breach(
    now: datetime, deadline: datetime, status: TicketStatus, sla_breached_at: datetime | None
) -> bool:
    """
    Check if a ticket is currently breaching the SLA.
    A breach occurs if now > deadline and the ticket is not terminal.
    If sla_breached_at is already set, we consider it still in breach state,
    but the monitor specifically looks for sla_breached_at IS NULL for idempotency.
    """
    if status in (TicketStatus.RESOLVED, TicketStatus.CLOSED):
        return False
    return now > deadline
