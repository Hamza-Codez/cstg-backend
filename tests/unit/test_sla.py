from datetime import UTC, datetime, timedelta

from app.domain.sla import deadline_for, duration, is_breach, is_sla_met
from app.models.enums import Priority, TicketStatus


def test_duration_per_priority() -> None:
    assert duration(Priority.CRITICAL) == timedelta(hours=2)
    assert duration(Priority.HIGH) == timedelta(hours=8)
    assert duration(Priority.MEDIUM) == timedelta(hours=24)
    assert duration(Priority.LOW) == timedelta(hours=72)


def test_sla_deadline_urgent() -> None:
    created_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    assert deadline_for(created_at, Priority.CRITICAL) == created_at + timedelta(hours=2)
    assert deadline_for(created_at, Priority.LOW) == created_at + timedelta(hours=72)


def test_met_at_deadline_vs_breach_after() -> None:
    created_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    deadline = deadline_for(created_at, Priority.CRITICAL)  # 14:00

    # Met at exactly deadline
    assert is_sla_met(resolved_at=deadline, deadline=deadline) is True

    # Not met after deadline
    assert is_sla_met(resolved_at=deadline + timedelta(seconds=1), deadline=deadline) is False

    # Met before deadline
    assert is_sla_met(resolved_at=deadline - timedelta(minutes=1), deadline=deadline) is True

    # Not met if unresolved (None)
    assert is_sla_met(resolved_at=None, deadline=deadline) is False


def test_is_breached() -> None:
    deadline = datetime(2026, 1, 1, 14, 0, 0, tzinfo=UTC)

    # Not in breach if before or at deadline
    assert is_breach(deadline, deadline, TicketStatus.OPEN, None) is False
    assert is_breach(deadline - timedelta(seconds=1), deadline, TicketStatus.OPEN, None) is False

    # In breach if after deadline and open
    assert is_breach(deadline + timedelta(seconds=1), deadline, TicketStatus.OPEN, None) is True

    # Not in breach if terminal, even if past deadline
    assert (
        is_breach(deadline + timedelta(seconds=1), deadline, TicketStatus.RESOLVED, None) is False
    )
    assert is_breach(deadline + timedelta(seconds=1), deadline, TicketStatus.CLOSED, None) is False
