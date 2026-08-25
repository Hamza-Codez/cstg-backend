"""Cursor encoding, including the search shape (spec04 §5)."""

import uuid
from datetime import UTC, datetime

import pytest

from app.core.pagination import (
    Cursor,
    InvalidCursor,
    decode_cursor,
    encode_cursor,
    require_shape,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
TID = uuid.UUID("11111111-2222-3333-4444-555555555555")


def test_two_part_cursor_round_trips() -> None:
    decoded = decode_cursor(encode_cursor(NOW, TID))
    assert decoded == Cursor(created_at=NOW, row_id=TID, rank=None)
    assert decoded.is_search is False


def test_three_part_search_cursor_round_trips() -> None:
    decoded = decode_cursor(encode_cursor(NOW, TID, rank=0.4218750298023224))
    assert decoded.created_at == NOW
    assert decoded.row_id == TID
    # repr() round-trips a float exactly, so paging never drifts on the sort key.
    assert decoded.rank == 0.4218750298023224
    assert decoded.is_search is True


def test_cursor_is_opaque_base64_without_padding() -> None:
    value = encode_cursor(NOW, TID)
    assert "=" not in value
    assert "|" not in value


@pytest.mark.parametrize(
    "bad",
    ["not-base64!!", "", "YWJj", "Zm9vfGJhcnxiYXp8cXV4"],
    ids=["invalid-chars", "empty", "too-few-parts", "too-many-parts"],
)
def test_malformed_cursors_are_rejected(bad: str) -> None:
    with pytest.raises(InvalidCursor):
        decode_cursor(bad)


def test_search_cursor_rejected_on_a_non_search_query() -> None:
    """The shapes order differently, so replaying one against the other would
    resume at a position that does not exist — silently skipping or repeating."""
    cursor = decode_cursor(encode_cursor(NOW, TID, rank=0.5))
    with pytest.raises(InvalidCursor, match="differently ordered"):
        require_shape(cursor, searching=False)


def test_plain_cursor_rejected_on_a_search_query() -> None:
    cursor = decode_cursor(encode_cursor(NOW, TID))
    with pytest.raises(InvalidCursor, match="differently ordered"):
        require_shape(cursor, searching=True)


def test_matching_shapes_are_accepted() -> None:
    require_shape(decode_cursor(encode_cursor(NOW, TID)), searching=False)
    require_shape(decode_cursor(encode_cursor(NOW, TID, rank=0.1)), searching=True)
