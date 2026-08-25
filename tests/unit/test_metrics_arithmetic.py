"""Metrics arithmetic (spec09 §8).

The service keeps every derivation out of SQL precisely so it can be tested
here, with no database. The two traps this file guards are the zero denominator
and the average-of-averages.
"""

from app.services.metrics_service import _average, _load, _metrics, _rate


def test_a_zero_denominator_is_zero_not_a_crash() -> None:
    # A desk with no resolved tickets yet must render 0%, not 500.
    assert _rate(0, 0) == 0.0
    assert _average(0.0, 0) == 0


def test_rate_and_average() -> None:
    assert _rate(3, 4) == 0.75
    assert _average(1000.0, 4) == 250


def test_averaging_group_averages_is_the_wrong_answer() -> None:
    """**The sum-then-divide rule.**

    Group A resolved 1 ticket in 100s; group B resolved 99 in 10,000s each. The
    true overall average is dominated by B. Averaging the two group averages
    would report roughly half that, and the dashboard would claim the desk is
    twice as fast as it is.
    """
    a_total, a_count = 100.0, 1
    b_total, b_count = 990_000.0, 99

    correct = _average(a_total + b_total, a_count + b_count)
    naive = (_average(a_total, a_count) + _average(b_total, b_count)) // 2

    assert correct == 9901
    assert naive == 5050
    assert correct != naive


def _row(**kwargs: object) -> dict:
    base = {
        "max_open_tickets": None,
        "open_tickets": 0,
        "in_progress": 0,
        "pending_customer": 0,
    }
    return {**base, **kwargs}


def test_no_ceiling_reports_none_not_zero() -> None:
    """None, not 0.0 — a bar chart draws 0.0 as "idle", which is a lie about an
    uncapped agent holding forty tickets."""
    assert _load(_row(max_open_tickets=None, open_tickets=40)) is None
    # A nonsensical ceiling is treated the same rather than dividing by zero.
    assert _load(_row(max_open_tickets=0, open_tickets=3)) is None


def test_load_counts_pending_customer_against_capacity() -> None:
    """A ticket waiting on a customer still occupies the agent, even though its
    SLA clock is paused — the same notion of "open" that `has_capacity` uses."""
    row = _row(max_open_tickets=10, open_tickets=3, in_progress=2, pending_customer=1)
    assert _load(row) == 0.6


def test_load_can_exceed_one_after_a_ceiling_is_lowered() -> None:
    # Not clamped: an over-capacity agent is exactly what the number is for.
    assert _load(_row(max_open_tickets=2, open_tickets=5)) == 2.5


def test_an_empty_group_is_all_zeroes_rather_than_absent() -> None:
    empty = _metrics(
        {
            "total": 0,
            "open": 0,
            "in_progress": 0,
            "pending_customer": 0,
            "resolved": 0,
            "closed": 0,
            "breached_open": 0,
            "breached_total": 0,
            "resolution_seconds_sum": 0.0,
            "working_seconds_sum": 0.0,
            "resolution_count": 0,
            "sla_met_count": 0,
        }
    )
    assert empty.breach_rate == 0.0
    assert empty.sla_met_rate == 0.0
    assert empty.avg_working_seconds == 0
