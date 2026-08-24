import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import ActorType, Category, EventType, Priority, TicketStatus


class TicketCreate(BaseModel):
    subject: str = Field(..., min_length=1, max_length=200)
    body: str = Field(..., min_length=1, max_length=10000)
    category: Category


class TicketResponse(BaseModel):
    id: uuid.UUID
    subject: str
    category: Category
    priority: Priority
    status: TicketStatus
    deadline: datetime
    escalation_level: int
    sla_breached_at: datetime | None
    created_at: datetime


class AssigneeSummary(BaseModel):
    """Just enough to name the owner; never exposes staff email or role."""

    id: uuid.UUID
    name: str


class TicketEventResponse(BaseModel):
    """One entry of the ticket timeline (docs/API.md §4).

    The `detail` jsonb is deliberately not exposed: it carries internal ids
    (e.g. assignee_id) that a customer must not see.
    """

    id: uuid.UUID
    type: EventType
    actor_type: ActorType
    from_status: TicketStatus | None
    to_status: TicketStatus | None
    created_at: datetime


class TicketDetailResponse(TicketResponse):
    """TicketResponse + body, assignee summary, and the visible timeline.

    Which events are "visible" is decided per principal in `ticket_service`.
    """

    body: str
    assignee: AssigneeSummary | None = None
    timeline: list[TicketEventResponse] = Field(default_factory=list)


class PaginatedTicketResponse(BaseModel):
    items: list[TicketResponse]
    next_cursor: str | None = None


class TransitionRequest(BaseModel):
    to: TicketStatus


class AssignmentRequest(BaseModel):
    assignee_id: uuid.UUID
