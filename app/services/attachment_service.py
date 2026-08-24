import asyncio
import uuid
from pathlib import Path
from typing import BinaryIO

from fastapi import UploadFile

from app.config import get_settings
from app.core.authorization import Principal, authorize_ticket_access
from app.core.clock import now
from app.core.unit_of_work import SqlAlchemyUnitOfWork
from app.domain.errors import BusinessRuleViolation, NotFound
from app.models.attachment import Attachment
from app.models.enums import Role

UPLOAD_DIR = Path("uploads")
_CHUNK_BYTES = 1024 * 1024


def _save_upload(source: BinaryIO, destination: Path, max_bytes: int) -> int:
    """Stream an upload to disk, enforcing the size cap as it writes.

    Runs in a worker thread: this process also hosts the SLA monitor
    (docs/ARCHITECTURE.md §6), so blocking the event loop on a large upload
    would delay breach detection. Checking the cap while streaming means an
    oversized file never lands on disk in full.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        with destination.open("wb") as out:
            while chunk := source.read(_CHUNK_BYTES):
                written += len(chunk)
                if written > max_bytes:
                    raise _UploadTooLarge(max_bytes)
                out.write(chunk)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return written


class _UploadTooLarge(Exception):
    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes


class AttachmentService:
    def __init__(self, uow: SqlAlchemyUnitOfWork):
        self.uow = uow

    async def upload_attachment(
        self, principal: Principal, ticket_id: uuid.UUID, file: UploadFile
    ) -> Attachment:
        """
        Uploads an attachment to a ticket.
        Only staff members (AGENT, DISPATCHER, ADMIN) are allowed to upload in v1.
        """
        if principal.role == Role.CUSTOMER:
            raise NotFound("Ticket not found")  # Hidden for v1 customers

        async with self.uow:
            # Check ticket existence and visibility
            ticket = await self.uow.tickets.get(ticket_id)
            if not ticket:
                raise NotFound("Ticket not found")

            if principal.role == Role.AGENT and ticket.assignee_id != principal.id:
                raise NotFound("Ticket not found")

            # Validate file (docs/API.md §8: max size + allowed content types).
            if not file.filename:
                raise BusinessRuleViolation("Filename is required")

            settings = get_settings()
            content_type = file.content_type or "application/octet-stream"
            if content_type not in settings.attachment_allowed_content_types:
                raise BusinessRuleViolation(f"Content type {content_type} is not allowed")

            attachment_id = uuid.uuid4()
            extension = Path(file.filename).suffix
            storage_path = UPLOAD_DIR / f"{attachment_id}{extension}"

            try:
                size = await asyncio.to_thread(
                    _save_upload, file.file, storage_path, settings.attachment_max_bytes
                )
            except _UploadTooLarge as exc:
                raise BusinessRuleViolation(
                    f"File exceeds the maximum size of {exc.max_bytes} bytes"
                ) from exc

            attachment = Attachment(
                id=attachment_id,
                ticket_id=ticket.id,
                filename=file.filename,
                content_type=content_type,
                size=size,
                storage_path=str(storage_path),
                created_at=now(),
            )
            self.uow.attachments.insert(attachment)

            return attachment

    async def get_attachment(
        self, principal: Principal, ticket_id: uuid.UUID, attachment_id: uuid.UUID
    ) -> Attachment:
        """
        Gets an attachment metadata, verifying visibility.
        """
        async with self.uow:
            ticket = await self.uow.tickets.get(ticket_id)
            if not ticket:
                raise NotFound("Attachment not found")

            # Object-level visibility, shared with every other read path
            # (AUTHORIZATION.md §4). Hidden resources surface as 404, not 403.
            authorize_ticket_access(principal, ticket)

            attachment = await self.uow.attachments.get(attachment_id)
            if not attachment or attachment.ticket_id != ticket.id:
                raise NotFound("Attachment not found")

            return attachment
