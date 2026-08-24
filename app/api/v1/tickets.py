import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_principal, get_db, get_storage
from app.core.authorization import Principal, require_roles
from app.core.pagination import InvalidCursor, decode_cursor
from app.core.storage import StorageBackend
from app.core.unit_of_work import SqlAlchemyUnitOfWork
from app.domain.errors import ValidationError
from app.models.enums import Category, CustomerTier, Role, TicketStatus
from app.models.enums import Priority as TicketPriority
from app.models.idempotency import IdempotencyKey
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
from app.schemas.ticket_filters import TicketFilters
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
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> TicketResponse:
    if idempotency_key:
        stmt = select(IdempotencyKey).where(
            IdempotencyKey.principal_id == principal.id, IdempotencyKey.key == idempotency_key
        )
        existing = (await db.execute(stmt)).scalar_one_or_none()
        if existing:
            return TicketResponse.model_validate(existing.response_body)

    async with service.uow:
        ticket = await service.create_ticket(principal, data)
        await db.flush()
        await db.refresh(ticket)
        resp = TicketResponse.model_validate(ticket, from_attributes=True)
        if idempotency_key:
            key_record = IdempotencyKey(
                principal_id=principal.id,
                key=idempotency_key,
                response_body=resp.model_dump(mode="json"),
                status_code=201,
            )
            db.add(key_record)
    return resp


@router.get(
    "",
    response_model=PaginatedTicketResponse,
)
async def list_tickets(
    principal: Annotated[Principal, Depends(get_current_principal)],
    service: Annotated[TicketService, Depends(get_ticket_service)],
    q: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    status_filter: Annotated[TicketStatus | None, Query(alias="status")] = None,
    priority: TicketPriority | None = None,
    category: Category | None = None,
    breached: bool | None = None,
    assigned: bool | None = None,
    escalated: bool | None = None,
    tier: CustomerTier | None = None,
    assignee_id: uuid.UUID | None = None,
    customer_id: uuid.UUID | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
) -> PaginatedTicketResponse:
    """Role-scoped search, filters and keyset pagination (docs/API.md §1, §4).

    Filters a principal may not use are refused 403 by the service — never
    silently dropped, which would answer a different question than the one asked
    (spec04 §4).
    """
    filters = TicketFilters(
        q=q,
        status=status_filter,
        priority=priority,
        category=category,
        breached=breached,
        assigned=assigned,
        escalated=escalated,
        tier=tier,
        assignee_id=assignee_id,
        customer_id=customer_id,
        created_after=created_after,
        created_before=created_before,
    )

    decoded = None
    if cursor:
        try:
            decoded = decode_cursor(cursor)
        except InvalidCursor as exc:
            raise ValidationError(str(exc)) from exc

    async with service.uow:
        tickets, next_cursor = await service.list_tickets(
            principal,
            filters=filters,
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
    # Coarse gate admits every authenticated role from P16: customers drive T5
    # (resume) and T6 (reopen). Which of T1-T6 each role may actually drive is
    # decided per-transition from the table in the service, so widening here
    # grants nothing on its own — a customer attempting T1/T2/T3 still gets 403.
    dependencies=[
        Depends(require_roles(Role.CUSTOMER, Role.AGENT, Role.DISPATCHER, Role.ADMIN))
    ],
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
    storage: Annotated[StorageBackend, Depends(get_storage)],
) -> AttachmentService:
    uow = SqlAlchemyUnitOfWork(session)
    return AttachmentService(uow, storage)


@router.post(
    "/{ticket_id}/comments",
    response_model=CommentResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(Role.AGENT, Role.DISPATCHER, Role.ADMIN, Role.CUSTOMER))],
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
    # P14: customers may upload to their own tickets — AUTHORIZATION.md §3
    # previously marked this "v2". The coarse gate admits every role; the
    # object-level gate in the service decides which ticket (INV-12).
    dependencies=[
        Depends(require_roles(Role.CUSTOMER, Role.AGENT, Role.DISPATCHER, Role.ADMIN))
    ],
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
    "/{ticket_id}/attachments",
    response_model=dict[str, list[AttachmentResponse]],
)
async def list_attachments(
    ticket_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    service: Annotated[AttachmentService, Depends(get_attachment_service)],
) -> dict[str, list[AttachmentResponse]]:
    """Attachments on a ticket (docs/API.md §8).

    Not paginated — the per-ticket cap bounds the list.
    """
    async with service.uow:
        attachments = await service.list_attachments(principal, ticket_id)
    return {
        "items": [AttachmentResponse.model_validate(a, from_attributes=True) for a in attachments]
    }


@router.get(
    "/{ticket_id}/attachments/{attachment_id}",
)
async def download_attachment(
    ticket_id: uuid.UUID,
    attachment_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    service: Annotated[AttachmentService, Depends(get_attachment_service)],
) -> StreamingResponse:
    """Stream the stored bytes.

    StreamingResponse over the storage port rather than FileResponse over a
    path: the port's contract is an opaque key, and a backend need not have a
    filesystem for this route to work.
    """
    async with service.uow:
        attachment = await service.get_attachment(principal, ticket_id, attachment_id)

    stream = await service.open_attachment(attachment)
    return StreamingResponse(
        stream,
        media_type=attachment.content_type,
        headers={
            # Always an attachment, never inline: the declared content type is
            # not verified against the bytes (spec03 §6), so nothing here may
            # render in the browser's origin.
            "Content-Disposition": f'attachment; filename="{attachment.filename}"',
            "Content-Length": str(attachment.size),
        },
    )
