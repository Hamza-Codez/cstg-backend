import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import TicketStatus
from app.schemas.common import ErrorDetail


class BulkAssignmentRequest(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ticket_ids: list[uuid.UUID]
    assignee_id: uuid.UUID
    override_capacity: bool = False


class BulkTransitionRequest(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ticket_ids: list[uuid.UUID]
    to: Literal[TicketStatus.CLOSED]  # Only CLOSED is allowed per spec10 (spec says "to": "CLOSED")


class BulkReassignmentRequest(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    from_assignee_id: uuid.UUID
    to_assignee_id: uuid.UUID
    statuses: list[TicketStatus] = Field(
        default_factory=lambda: [TicketStatus.OPEN, TicketStatus.IN_PROGRESS]
    )


class BulkItemResult(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ticket_id: uuid.UUID
    ok: bool
    error: ErrorDetail | None = None


class BulkResult(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    requested: int
    succeeded: int
    failed: int
    results: list[BulkItemResult]
