"""Opaque cursor encoding for keyset pagination (docs/API.md §1).

Keyset, not OFFSET: tickets are created continuously, and an offset page would
skip or repeat rows as new ones arrive. The cursor carries the sort key of the
last row seen — `(created_at, id)` — so the next page resumes exactly there.

The value is base64 and documented as opaque; clients must round-trip it
unchanged rather than construct one.
"""

import base64
import binascii
import uuid
from datetime import datetime


class InvalidCursor(ValueError):
    """Raised when a cursor cannot be decoded; callers map this to 400."""


def encode_cursor(created_at: datetime, ticket_id: uuid.UUID) -> str:
    raw = f"{created_at.isoformat()}|{ticket_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(cursor + padding).decode()
        timestamp, _, identifier = raw.partition("|")
        return datetime.fromisoformat(timestamp), uuid.UUID(identifier)
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise InvalidCursor("Malformed pagination cursor") from exc
