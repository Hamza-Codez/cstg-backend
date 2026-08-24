from datetime import UTC, datetime

from app.core import clock


def test_frozen_clock() -> None:
    now_dt = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
    frozen = clock.FrozenClock(now_dt)
    clock.set_clock(frozen)
    try:
        assert clock.now() == now_dt

        # Test time doesn't advance
        import time

        time.sleep(0.01)
        assert clock.now() == now_dt
    finally:
        clock.reset_clock()

    # Outside block, clock is normal
    assert clock.now() != now_dt
    assert clock.now() > now_dt
