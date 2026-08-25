"""Notification contract (docs/API.md §14)."""

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.models.enums import ActorType, EventType, TicketStatus


class NotificationItem(BaseModel):
    event_id: uuid.UUID
    ticket_id: uuid.UUID
    #: Joined in so the client renders a line without an N+1 lookup.
    ticket_subject: str
    type: EventType
    actor_type: ActorType
    #: None for SYSTEM — the SLA monitor and automated assignment.
    actor_name: str | None = None
    from_status: TicketStatus | None = None
    to_status: TicketStatus | None = None
    created_at: datetime
    #: Whether this one predates the recipient's read cursor. Derived server
    #: side so the client never has to reason about clock skew against
    #: `last_read_at` to decide what to mark.
    read: bool = False


class NotificationPage(BaseModel):
    """Recent notifications, read and unread.

    Read and unread together since P21: the panel is a history, not a queue, so
    opening it no longer empties it.
    """

    items: list[NotificationItem]
    unread_count: int
    last_read_at: datetime
    #: Opaque keyset cursor for the next (older) page; None at the end.
    next_cursor: str | None = None


class NotificationCount(BaseModel):
    """The cheap badge-only shape, so the frequent poll stays small."""

    unread_count: int


class MarkReadRequest(BaseModel):
    up_to: datetime | None = None
