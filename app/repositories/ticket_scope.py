"""The one definition of "which tickets may this principal see" (spec08 §4).

**Extraction, not duplication.** Notifications are a second read path over
`ticket_event`, and a second read path is a second chance to leak. If the two
call sites each carried their own copy of the scope rule, a later change to a
role's visibility would be applied in one and forgotten in the other — and the
forgotten one would be the notification feed, which nobody looks at until it has
already disclosed something.

Both `TicketService.list_tickets` and the notification query import from here, so
a change to the rule cannot reach one without reaching the other.

The rule itself is AUTHORIZATION.md §3 / INV-9: customers see their own tickets,
agents see what is assigned to them, dispatchers and admins see everything.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import ColumnElement, true

from app.core.authorization import Principal
from app.models.enums import ActorType, Role
from app.models.ticket import Ticket


@dataclass(frozen=True)
class TicketScope:
    """A principal's read scope, in the two shapes callers need.

    `columns` is for the keyword-argument style `list_scoped` already uses;
    `predicate` is for queries that build their own WHERE clause. Both are
    derived from the same decision, so they cannot disagree.
    """

    customer_id: uuid.UUID | None = None
    assignee_id: uuid.UUID | None = None

    @property
    def unrestricted(self) -> bool:
        return self.customer_id is None and self.assignee_id is None

    def columns(self) -> dict[str, uuid.UUID]:
        if self.customer_id is not None:
            return {"customer_id": self.customer_id}
        if self.assignee_id is not None:
            return {"assignee_id": self.assignee_id}
        return {}

    def predicate(self) -> ColumnElement[bool]:
        """A WHERE fragment over `ticket`, for queries that join to it."""
        if self.customer_id is not None:
            return Ticket.customer_id == self.customer_id
        if self.assignee_id is not None:
            return Ticket.assignee_id == self.assignee_id
        return true()


def scope_for(principal: Principal) -> TicketScope:
    """Resolve a principal's ticket visibility (INV-9)."""
    if principal.type is ActorType.CUSTOMER:
        return TicketScope(customer_id=principal.id)
    if principal.role is Role.AGENT:
        return TicketScope(assignee_id=principal.id)
    # DISPATCHER and ADMIN see everything.
    return TicketScope()
