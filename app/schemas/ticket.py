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
    # The frozen original promise (INV-1/INV-2). Kept in the response as the
    # record of "what we committed to", but it is NOT what a countdown reads —
    # see sla_due_at (spec05 §8).
    deadline: datetime
    # The current effective due time. This is the countdown's source.
    sla_due_at: datetime
    sla_paused_at: datetime | None
    sla_paused_seconds: int
    reopen_count: int
    #: The policy this ticket's window was computed under (INV-15). Lets the
    #: UI explain a frozen deadline rather than leave it looking like a bug.
    sla_policy_version_id: uuid.UUID
    resolved_at: datetime | None
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
    #: The duration this ticket's priority carried under its pinned policy.
    #: Embedded so the detail screen can say "2 hours, under the policy active
    #: when this was created" without a second request.
    sla_policy_seconds: int | None = None
    sla_policy_activated_at: datetime | None = None


class PaginatedTicketResponse(BaseModel):
    items: list[TicketResponse]
    next_cursor: str | None = None


class TransitionRequest(BaseModel):
    to: TicketStatus


class AssignmentRequest(BaseModel):
    assignee_id: uuid.UUID
    #: Capacity is a routing heuristic, not an invariant — a dispatcher handling
    #: a CRITICAL outage must be able to say "anyway" (spec07 §6). Recorded in
    #: the ASSIGNMENT event's detail.
    override_capacity: bool = False
