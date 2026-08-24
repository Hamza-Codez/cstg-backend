from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import Request

from app.domain.errors import Forbidden, NotFound, Unauthenticated
from app.models.enums import ActorType, Role


@dataclass(frozen=True)
class Principal:
    id: UUID
    type: ActorType
    role: Role
    is_active: bool


def require_roles(*roles: Role) -> Callable[[Request], Principal]:
    """
    FastAPI dependency to enforce coarse role-based access control.
    Retrieves the principal injected into the request state by the auth middleware/dependency.
    """

    def role_checker(request: Request) -> Principal:
        if not hasattr(request.state, "principal"):
            raise Unauthenticated("Not authenticated.")

        principal: Principal = request.state.principal
        if principal.role not in roles:
            raise Forbidden("Role not permitted for this action.")
        return principal

    return role_checker


def authorize_ticket_access(principal: Principal, ticket: Any) -> None:
    """
    Object-level authorization check for ticket access (read/write).
    If a user is not permitted to know the ticket exists, this raises NotFound (404)
    rather than Forbidden (403) per API.md §2 and AUTHORIZATION.md §4.
    """
    if principal.role in (Role.ADMIN, Role.DISPATCHER):
        return

    if principal.type == ActorType.CUSTOMER:
        if ticket.customer_id != principal.id:
            raise NotFound("Ticket not found.")
        return

    if principal.role == Role.AGENT:
        if ticket.assignee_id != principal.id:
            raise NotFound("Ticket not found.")
        return

    raise Forbidden("Role not permitted for this action.")
