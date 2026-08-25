import uuid

from fastapi import APIRouter, Depends, Request

from app.api.deps import get_current_principal
from app.config import get_settings
from app.core.authorization import Principal, require_roles
from app.domain.errors import ValidationError
from app.models.enums import Role
from app.schemas.bulk import (
    BulkAssignmentRequest,
    BulkReassignmentRequest,
    BulkResult,
    BulkTransitionRequest,
)
from app.services.bulk_service import BulkService

router = APIRouter(
    prefix="/tickets/bulk",
    tags=["Bulk Operations"],
    dependencies=[Depends(get_current_principal)],
)


def _validate_ticket_ids(ticket_ids: list[uuid.UUID]) -> None:
    settings = get_settings()
    if not ticket_ids:
        raise ValidationError("Request must contain at least one ticket ID.")
    if len(ticket_ids) > settings.bulk_max_items:
        raise ValidationError(f"Request cannot exceed {settings.bulk_max_items} items.")
    if len(set(ticket_ids)) != len(ticket_ids):
        raise ValidationError("Duplicate ticket IDs are not permitted.")


@router.post(
    "/assignment",
    response_model=BulkResult,
    dependencies=[Depends(require_roles(Role.AGENT, Role.DISPATCHER, Role.ADMIN))],
)
async def bulk_assign(
    request: Request,
    body: BulkAssignmentRequest,
    principal: Principal = Depends(get_current_principal),
) -> BulkResult:
    """Assign multiple tickets in a single request.

    Capacity is evaluated per item as the batch proceeds, not once up front.
    """
    _validate_ticket_ids(body.ticket_ids)

    service = BulkService(request.app.state.session_factory)
    return await service.assign_tickets(principal, body)


@router.post(
    "/transitions",
    response_model=BulkResult,
    dependencies=[Depends(require_roles(Role.AGENT, Role.DISPATCHER, Role.ADMIN))],
)
async def bulk_transition(
    request: Request,
    body: BulkTransitionRequest,
    principal: Principal = Depends(get_current_principal),
) -> BulkResult:
    """Transition multiple tickets. Only transition to CLOSED is permitted."""
    _validate_ticket_ids(body.ticket_ids)

    service = BulkService(request.app.state.session_factory)
    return await service.transition_tickets(principal, body)


@router.post(
    "/reassignment",
    response_model=BulkResult,
    dependencies=[Depends(require_roles(Role.AGENT, Role.DISPATCHER, Role.ADMIN))],
)
async def bulk_reassign(
    request: Request,
    body: BulkReassignmentRequest,
    principal: Principal = Depends(get_current_principal),
) -> BulkResult:
    """Reassign an agent's queue.

    Selection is server-side by current assignee and statuses.
    """
    service = BulkService(request.app.state.session_factory)
    return await service.reassign_tickets(principal, body)
