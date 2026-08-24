import uuid
from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import BinaryIO

from fastapi import UploadFile

from app.config import get_settings
from app.core.authorization import Principal, authorize_ticket_access
from app.core.clock import now
from app.core.storage import StorageBackend, StorageTooLarge
from app.core.unit_of_work import SqlAlchemyUnitOfWork
from app.domain.errors import BusinessRuleViolation, NotFound
from app.models.attachment import Attachment
from app.models.enums import ActorType, EventType, TicketStatus
from app.models.ticket import Ticket
from app.models.ticket_event import TicketEvent

UPLOAD_PREFIX = "uploads"


class AttachmentService:
    """Attachment metadata and bytes.

    Bytes go through the injected ``StorageBackend`` (spec03 §3); this service
    never touches a filesystem, so swapping to object storage changes nothing
    here.
    """

    def __init__(self, uow: SqlAlchemyUnitOfWork, storage: StorageBackend):
        self.uow = uow
        self.storage = storage

    async def _authorized_ticket(self, principal: Principal, ticket_id: uuid.UUID) -> Ticket:
        """The ticket, if this principal may reach it — otherwise 404.

        INV-12: every attachment path resolves access through the *parent
        ticket*, using the same helper as every other read. There is no
        attachment-level permission and no independent access path, so an
        attachment id is meaningless without access to its ticket.
        """
        ticket = await self.uow.tickets.get(ticket_id)
        if not ticket:
            raise NotFound("Ticket not found")
        authorize_ticket_access(principal, ticket)
        return ticket

    async def upload_attachment(
        self, principal: Principal, ticket_id: uuid.UUID, file: UploadFile
    ) -> Attachment:
        """Store an attachment's bytes and its metadata, plus one audit event.

        Customers may upload to their own tickets from P14 (AUTHORIZATION.md §3
        previously marked this "v2"). Authorization is the parent ticket's, via
        ``_authorized_ticket``.
        """
        ticket = await self._authorized_ticket(principal, ticket_id)

        # A closed ticket takes no new material, matching the rule spec02 set
        # for replies.
        if ticket.status is TicketStatus.CLOSED:
            raise BusinessRuleViolation("This ticket is closed and cannot take new attachments")

        if not file.filename:
            raise BusinessRuleViolation("Filename is required")

        settings = get_settings()

        # The declared content type is checked against the allow-list; the
        # stored value is the declared one. Sniffing the real type is out of
        # scope (spec03 §6) — what protects the client is the non-inline
        # disposition on download, not this check.
        content_type = file.content_type or "application/octet-stream"
        if content_type not in settings.attachment_allowed_content_types:
            raise BusinessRuleViolation(f"Content type {content_type} is not allowed")

        existing = len(await self.uow.attachments.list_for_ticket(ticket_id))
        if existing >= settings.attachment_max_per_ticket:
            raise BusinessRuleViolation(
                f"This ticket already has the maximum of "
                f"{settings.attachment_max_per_ticket} attachments"
            )

        attachment_id = uuid.uuid4()
        extension = PurePosixPath(file.filename).suffix
        key = f"{UPLOAD_PREFIX}/{attachment_id}{extension}"

        try:
            size = await self.storage.put(key, file.file, settings.attachment_max_bytes)
        except StorageTooLarge as exc:
            raise BusinessRuleViolation(
                f"File exceeds the maximum size of {exc.max_bytes} bytes"
            ) from exc

        # Bytes are written before the row, so a failure here would leave an
        # object nothing references. Delete it rather than leak it — this is the
        # one cleanup the transaction cannot do for us (P14.4).
        try:
            async with self.uow:
                attachment = Attachment(
                    id=attachment_id,
                    ticket_id=ticket_id,
                    filename=file.filename,
                    content_type=content_type,
                    size=size,
                    storage_path=key,
                    created_at=now(),
                )
                self.uow.attachments.insert(attachment)

                # INV-5: upload was the one state change with no audit record.
                self.uow.events.insert(
                    TicketEvent(
                        id=uuid.uuid4(),
                        ticket_id=ticket_id,
                        type=EventType.ATTACHMENT,
                        actor_type=(
                            ActorType.CUSTOMER
                            if principal.type is ActorType.CUSTOMER
                            else ActorType.USER
                        ),
                        actor_id=principal.id,
                        detail={
                            "attachment_id": str(attachment_id),
                            "filename": file.filename,
                            "size": size,
                        },
                        created_at=now(),
                    )
                )
        except BaseException:
            await self.storage.delete(key)
            raise

        return attachment

    async def list_attachments(
        self, principal: Principal, ticket_id: uuid.UUID
    ) -> Sequence[Attachment]:
        """Attachments on a ticket the principal may read.

        Not paginated: the per-ticket cap bounds the list (spec03 §7).
        """
        await self._authorized_ticket(principal, ticket_id)
        return await self.uow.attachments.list_for_ticket(ticket_id)

    async def get_attachment(
        self, principal: Principal, ticket_id: uuid.UUID, attachment_id: uuid.UUID
    ) -> Attachment:
        """Attachment metadata, authorized through its parent ticket."""
        await self._authorized_ticket(principal, ticket_id)

        attachment = await self.uow.attachments.get(attachment_id)
        # The pair must match: an attachment reached through the wrong ticket is
        # 404, so the nested route cannot lie about its own shape (spec03 §7).
        if not attachment or attachment.ticket_id != ticket_id:
            raise NotFound("Attachment not found")
        return attachment

    async def open_attachment(self, attachment: Attachment) -> BinaryIO:
        """Open the stored bytes. Caller has already been authorized."""
        return await self.storage.open(attachment.storage_path)
