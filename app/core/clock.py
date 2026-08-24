"""Injectable time source.

Every deadline, breach check, and audit timestamp reads the clock through this module
rather than calling ``datetime.now`` directly, so tests can freeze time and the SLA
boundary cases in docs/TESTING.md §5 stay deterministic.

P0 provides the swap mechanism because the pytest harness needs a freezable clock.
P5 wires it into the SLA monitor.
"""

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    """Real time, always timezone-aware UTC (the database stores ``timestamptz``)."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """Fixed time for tests. Advance explicitly to cross an SLA boundary."""

    def __init__(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("FrozenClock requires a timezone-aware datetime")
        self._at = at

    def now(self) -> datetime:
        return self._at

    def set(self, at: datetime) -> None:
        self._at = at


_clock: Clock = SystemClock()


def now() -> datetime:
    """The current time according to the active clock."""
    return _clock.now()


def set_clock(clock: Clock) -> None:
    """Install a clock. Tests use this via the ``frozen_clock`` fixture."""
    global _clock
    _clock = clock


def reset_clock() -> None:
    """Restore the real clock."""
    global _clock
    _clock = SystemClock()
