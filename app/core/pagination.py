"""Opaque cursor encoding for keyset pagination (docs/API.md §1).

Keyset, not OFFSET: tickets are created continuously, and an offset page would
skip or repeat rows as new ones arrive. The cursor carries the sort key of the
last row seen so the next page resumes exactly there.

Two shapes, because search sorts differently (spec04 §5):

- ``created_at | id``          — the default ordering
- ``rank | created_at | id``   — full-text search, ordered by relevance first

Relevance cannot be keyset-paginated on its own: ``ts_rank_cd`` is not stored,
unique, or monotonic. Carrying the rank in the cursor keeps the
"pages neither skip nor repeat" guarantee that motivated keyset in the first
place, without falling back to OFFSET.

The value is base64 and documented as opaque; clients round-trip it unchanged
rather than construct one.
"""

import base64
import binascii
import uuid
from dataclasses import dataclass
from datetime import datetime


class InvalidCursor(ValueError):
    """Raised when a cursor cannot be decoded; callers map this to 400."""


@dataclass(frozen=True)
class Cursor:
    """The sort key of the last row on the previous page.

    `row_id`, not `ticket_id`: a keyset cursor carries whatever row the query
    returned. Ticket listing puts a ticket id here; the notification feed puts
    a `ticket_event` id. The wire format is the same either way.
    """

    created_at: datetime
    row_id: uuid.UUID
    #: Present only for search results. Its presence must match the query shape —
    #: see ``require_shape``.
    rank: float | None = None

    @property
    def is_search(self) -> bool:
        return self.rank is not None


def encode_cursor(created_at: datetime, row_id: uuid.UUID, rank: float | None = None) -> str:
    parts = [created_at.isoformat(), str(row_id)]
    if rank is not None:
        parts.insert(0, repr(rank))
    raw = "|".join(parts).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> Cursor:
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(cursor + padding).decode()
        parts = raw.split("|")
        if len(parts) == 2:
            timestamp, identifier = parts
            return Cursor(datetime.fromisoformat(timestamp), uuid.UUID(identifier))
        if len(parts) == 3:
            rank, timestamp, identifier = parts
            return Cursor(
                datetime.fromisoformat(timestamp), uuid.UUID(identifier), rank=float(rank)
            )
        raise InvalidCursor("Malformed pagination cursor")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        # InvalidCursor is a ValueError, so re-raising it unchanged keeps the
        # specific message rather than replacing it with the generic one.
        if isinstance(exc, InvalidCursor):
            raise
        raise InvalidCursor("Malformed pagination cursor") from exc


def require_shape(cursor: Cursor, *, searching: bool) -> None:
    """Reject a cursor that belongs to a differently-ordered query.

    A search cursor replayed against an unfiltered list — or the reverse — would
    resume from a position that does not exist in the new ordering, silently
    skipping or repeating rows. Rejecting is the only honest answer; the client
    must start from the first page when it changes the query shape.
    """
    if cursor.is_search != searching:
        raise InvalidCursor(
            "This cursor belongs to a differently ordered query. Start from the first page."
        )
