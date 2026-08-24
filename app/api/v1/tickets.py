import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_principal, get_db
from app.core.authorization import Principal, require_roles
from app.core.pagination import InvalidCursor, decode_cursor
from app.core.unit_of_work import SqlAlchemyUnitOfWork
from app.domain.errors import ValidationError
from app.models.enums import Priority as TicketPriority
from app.models.enums import Role, TicketStatus
from app.schemas.attachment import AttachmentResponse
from app.schemas.comment import CommentCreate, CommentResponse
from app.schemas.ticket import (
    AssigneeSummary,
    AssignmentRequest,
    PaginatedTicketResponse,
    TicketCreate,
    TicketDetailResponse,
    TicketEventResponse,
    TicketResponse,
    TransitionRequest,
)
from app.services.assignment_service import AssignmentService
from app.services.attachment_service import AttachmentService
from app.services.comment_service import CommentService
from app.services.ticket_service import TicketService

router = APIRouter(
    prefix="/tickets", tags=["Tickets"], dependencies=[Depends(get_current_principal)]
)


async def get_ticket_service(session: Annotated[AsyncSession, Depends(get_db)]) -> TicketService:
    uow = SqlAlchemyUnitOfWork(session)
    return TicketService(uow)


async def get_assignment_service(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> AssignmentService:
    uow = SqlAlchemyUnitOfWork(session)
    return AssignmentService(uow)


@router.post(
    "",
    response_model=TicketResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(Role.CUSTOMER))],
)
async def create_ticket(
    data: TicketCreate,
    principal: Annotated[Principal, Depends(get_current_principal)],
    service: Annotated[TicketService, Depends(get_ticket_service)],
) -> TicketResponse:
    async with service.uow:
        ticket = await service.create_ticket(principal, data)
    return TicketResponse.model_validate(ticket, from_attributes=True)


@router.get(
    "",
    response_model=PaginatedTicketResponse,
)
async def list_tickets(
    principal: Annotated[Principal, Depends(get_current_principal)],
    service: Annotated[TicketService, Depends(get_ticket_service)],
    status_filter: Annotated[TicketStatus | None, Query(alias="status")] = None,
    priority: TicketPriority | None = None,
    breached: bool | None = None,
    assigned: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
) -> PaginatedTicketResponse:
    """Role-scoped list with filters and keyset pagination (docs/API.md §1, §4)."""
    decoded = None
    if cursor:
        try:
            decoded = decode_cursor(cursor)
        except InvalidCursor as exc:
            raise ValidationError("Malformed pagination cursor") from exc

    async with service.uow:
        tickets, next_cursor = await service.list_tickets(
            principal,
            status=status_filter,
            priority=priority,
            breached=breached,
            assigned=assigned,
            limit=limit,
            cursor=decoded,
        )
    return PaginatedTicketResponse(
        items=[TicketResponse.model_validate(t, from_attributes=True) for t in tickets],
        next_cursor=next_cursor,
    )


@router.get(
    "/{ticket_id}",
    response_model=TicketDetailResponse,
)
async def get_ticket(
    ticket_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    service: Annotated[TicketService, Depends(get_ticket_service)],
) -> TicketDetailResponse:
    async with service.uow:
        ticket, events = await service.get_ticket_detail(principal, ticket_id)
        assignee = None
        if ticket.assignee_id is not None:
            owner = await service.uow.users.get(ticket.assignee_id)
            if owner is not None:
                assignee = AssigneeSummary(id=owner.id, name=owner.name)

    return TicketDetailResponse(
        **TicketResponse.model_validate(ticket, from_attributes=True).model_dump(),
        body=ticket.body,
        assignee=assignee,
        timeline=[TicketEventResponse.model_validate(e, from_attributes=True) for e in events],
    )


@router.post(
    "/{ticket_id}/transitions",
    response_model=TicketResponse,
    # Coarse gate: customers drive no transitions (AUTHORIZATION.md §3). Which of
    # T1-T3 each staff role may drive is decided per-transition in the service.
    dependencies=[Depends(require_roles(Role.AGENT, Role.DISPATCHER, Role.ADMIN))],
)
async def transition_ticket(
    ticket_id: uuid.UUID,
    data: TransitionRequest,
    principal: Annotated[Principal, Depends(get_current_principal)],
    service: Annotated[TicketService, Depends(get_ticket_service)],
) -> TicketResponse:
    async with service.uow:
        ticket = await service.transition_ticket(principal, ticket_id, data.to)
    return TicketResponse.model_validate(ticket, from_attributes=True)


@router.post(
    "/{ticket_id}/assignment",
    response_model=TicketResponse,
    dependencies=[Depends(require_roles(Role.DISPATCHER, Role.ADMIN))],
)
async def assign_ticket(
    ticket_id: uuid.UUID,
    data: AssignmentRequest,
    principal: Annotated[Principal, Depends(get_current_principal)],
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
) -> TicketResponse:
    async with service.uow:
        ticket = await service.assign_ticket(principal, ticket_id, data.assignee_id)
    return TicketResponse.model_validate(ticket, from_attributes=True)


async def get_comment_service(session: Annotated[AsyncSession, Depends(get_db)]) -> CommentService:
    uow = SqlAlchemyUnitOfWork(session)
    return CommentService(uow)


async def get_attachment_service(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> AttachmentService:
    uow = SqlAlchemyUnitOfWork(session)
    return AttachmentService(uow)


@router.post(
    "/{ticket_id}/comments",
    response_model=CommentResponse,
    status_code=status.HTTP_201_CREATED,
    # Staff author comments and upload files; customers do neither in v1
    # (AUTHORIZATION.md §3).
    dependencies=[Depends(require_roles(Role.AGENT, Role.DISPATCHER, Role.ADMIN))],
)
async def create_comment(
    ticket_id: uuid.UUID,
    data: CommentCreate,
    principal: Annotated[Principal, Depends(get_current_principal)],
    service: Annotated[CommentService, Depends(get_comment_service)],
) -> CommentResponse:
    async with service.uow:
        comment = await service.add_comment(principal, ticket_id, data)
    return CommentResponse.model_validate(comment, from_attributes=True)


@router.get(
    "/{ticket_id}/comments",
    response_model=dict[str, list[CommentResponse]],
)
async def list_comments(
    ticket_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    service: Annotated[CommentService, Depends(get_comment_service)],
) -> dict[str, list[CommentResponse]]:
    async with service.uow:
        comments = await service.get_ticket_comments(principal, ticket_id)
    return {"items": [CommentResponse.model_validate(c, from_attributes=True) for c in comments]}


@router.post(
    "/{ticket_id}/attachments",
    response_model=AttachmentResponse,
    status_code=status.HTTP_201_CREATED,
    # Staff author comments and upload files; customers do neither in v1
    # (AUTHORIZATION.md §3).
    dependencies=[Depends(require_roles(Role.AGENT, Role.DISPATCHER, Role.ADMIN))],
)
async def upload_attachment(
    ticket_id: uuid.UUID,
    file: UploadFile,
    principal: Annotated[Principal, Depends(get_current_principal)],
    service: Annotated[AttachmentService, Depends(get_attachment_service)],
) -> AttachmentResponse:
    async with service.uow:
        attachment = await service.upload_attachment(principal, ticket_id, file)
    return AttachmentResponse.model_validate(attachment, from_attributes=True)


@router.get(
    "/{ticket_id}/attachments/{attachment_id}",
)
async def download_attachment(
    ticket_id: uuid.UUID,
    attachment_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    service: Annotated[AttachmentService, Depends(get_attachment_service)],
) -> FileResponse:
    async with service.uow:
        attachment = await service.get_attachment(principal, ticket_id, attachment_id)

    return FileResponse(
        path=attachment.storage_path,
        filename=attachment.filename,
        media_type=attachment.content_type,
    )
