"""Ticket list filters and the role gate over them (spec04 §4).

One model, used by three callers: the list endpoint, saved-view creation, and
saved-view execution. Sharing it is what makes the role rule impossible to apply
in one place and forget in another — roles change, so a view saved as a
dispatcher must not keep granting dispatcher-only filters afterwards.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.core.authorization import Principal
from app.domain.errors import Forbidden
from app.models.enums import ActorType, Category, CustomerTier, Priority, Role, TicketStatus

#: Filters that reach beyond a principal's own work. A customer or agent asking
#: for these is asking about other people's tickets.
STAFF_WIDE_FILTERS = ("tier", "assignee_id", "customer_id")


class TicketFilters(BaseModel):
    """Every filter the list endpoint accepts. All optional, all combinable."""

    model_config = ConfigDict(extra="forbid")

    q: str | None = Field(default=None, min_length=1, max_length=200)
    status: TicketStatus | None = None
    priority: Priority | None = None
    category: Category | None = None
    breached: bool | None = None
    assigned: bool | None = None
    escalated: bool | None = None
    tier: CustomerTier | None = None
    assignee_id: uuid.UUID | None = None
    customer_id: uuid.UUID | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None

    @property
    def is_search(self) -> bool:
        return self.q is not None


def authorize_filters(principal: Principal, filters: TicketFilters) -> None:
    """Reject filters this principal may not use — **403, never silently dropped**.

    Dropping is the subtler bug: a customer who passes another customer's
    ``customer_id`` would get *their own* tickets back, which reads as a
    successful answer to the question they asked. An empty list would read as
    "that customer has no tickets", which is itself a disclosure. Only an
    explicit refusal is honest.

    This is a capability check, not existence-hiding, so it is 403 rather than
    the 404 that INV-9 mandates for a specific hidden resource.
    """
    if principal.role in (Role.DISPATCHER, Role.ADMIN):
        return

    used = [name for name in STAFF_WIDE_FILTERS if getattr(filters, name) is not None]
    if used:
        raise Forbidden(f"Not permitted to filter by {', '.join(sorted(used))}.")

    # An agent's scope is already "assigned to me" and a customer's is "mine",
    # so `assigned` is meaningless for them and would only ever be confusing.
    if filters.assigned is not None and principal.type is ActorType.CUSTOMER:
        raise Forbidden("Not permitted to filter by assigned.")
