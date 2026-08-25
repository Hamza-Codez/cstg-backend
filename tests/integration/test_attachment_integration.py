"""Attachment service against a real database (spec03 §9).

Covers what the API tier cannot see: that bytes and rows commit together, and
that a failure on either side leaves neither behind.
"""

import io
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authorization import Principal
from app.core.storage import MemoryStorage, StorageKeyNotFound
from app.core.unit_of_work import SqlAlchemyUnitOfWork
from app.domain.errors import BusinessRuleViolation
from app.models.attachment import Attachment
from app.models.enums import ActorType, Category, EventType, Role
from app.models.ticket_event import TicketEvent
from app.schemas.ticket import TicketCreate
from app.services.attachment_service import AttachmentService
from app.services.ticket_service import TicketService
from tests.api.test_tickets import create_customer, seed_priority_rules


def _upload(name: str = "notes.txt", body: bytes = b"payload") -> UploadFile:
    return UploadFile(filename=name, file=io.BytesIO(body), headers={"content-type": "text/plain"})


async def _customer_with_ticket(
    db_session: AsyncSession, session_factory: Any, email: str
) -> tuple[Principal, Any]:
    customer = await create_customer(db_session, email)
    await seed_priority_rules(db_session)
    principal = Principal(
        id=customer.id, type=ActorType.CUSTOMER, role=Role.CUSTOMER, is_active=True
    )

    async with session_factory() as session:
        service = TicketService(SqlAlchemyUnitOfWork(session))
        async with service.uow:
            ticket = await service.create_ticket(
                principal,
                TicketCreate(subject="S", body="B", category=Category.GENERAL),
            )
            ticket_id = ticket.id
    return principal, ticket_id


@pytest.mark.db
async def test_upload_writes_row_and_event_in_one_transaction(
    db_session: AsyncSession, session_factory: Any
) -> None:
    """INV-5 — upload was the one state change with no audit record until P14."""
    principal, ticket_id = await _customer_with_ticket(
        db_session, session_factory, "int_att_ok@example.com"
    )
    storage = MemoryStorage()

    async with session_factory() as session:
        service = AttachmentService(SqlAlchemyUnitOfWork(session), storage)
        attachment = await service.upload_attachment(principal, ticket_id, _upload())

    rows = (
        (await db_session.execute(select(Attachment).where(Attachment.ticket_id == ticket_id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1

    events = (
        (
            await db_session.execute(
                select(TicketEvent).where(
                    TicketEvent.ticket_id == ticket_id,
                    TicketEvent.type == EventType.ATTACHMENT,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].actor_type is ActorType.CUSTOMER
    assert events[0].actor_id == principal.id
    assert events[0].detail["attachment_id"] == str(attachment.id)

    # The bytes are reachable under the key the row records.
    with await storage.open(attachment.storage_path) as fh:
        assert fh.read() == b"payload"


@pytest.mark.db
async def test_failed_persist_removes_the_stored_bytes(
    db_session: AsyncSession, session_factory: Any
) -> None:
    """No orphans (P14.4).

    Bytes are written before the row, so a failure between the two would leave
    an object nothing references. The transaction cannot clean that up — the
    service must.
    """
    principal, ticket_id = await _customer_with_ticket(
        db_session, session_factory, "int_att_orphan@example.com"
    )
    storage = MemoryStorage()

    with patch("app.services.attachment_service.TicketEvent") as mock_event:
        mock_event.side_effect = ValueError("Forced audit failure")

        with pytest.raises(ValueError, match="Forced audit failure"):
            async with session_factory() as session:
                service = AttachmentService(SqlAlchemyUnitOfWork(session), storage)
                await service.upload_attachment(principal, ticket_id, _upload())

    rows = (
        (await db_session.execute(select(Attachment).where(Attachment.ticket_id == ticket_id)))
        .scalars()
        .all()
    )
    assert rows == [], "attachment row must roll back"

    assert storage._objects == {}, "stored bytes must not outlive the failed transaction"


@pytest.mark.db
async def test_oversize_upload_leaves_no_row_and_no_bytes(
    db_session: AsyncSession, session_factory: Any
) -> None:
    principal, ticket_id = await _customer_with_ticket(
        db_session, session_factory, "int_att_big@example.com"
    )
    storage = MemoryStorage()

    with patch("app.services.attachment_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.attachment_allowed_content_types = ("text/plain",)
        settings.attachment_max_bytes = 10
        settings.attachment_max_per_ticket = 20

        async with session_factory() as session:
            service = AttachmentService(SqlAlchemyUnitOfWork(session), storage)
            with pytest.raises(BusinessRuleViolation, match="exceeds the maximum size"):
                await service.upload_attachment(principal, ticket_id, _upload(body=b"x" * 5000))

    rows = (
        (await db_session.execute(select(Attachment).where(Attachment.ticket_id == ticket_id)))
        .scalars()
        .all()
    )
    assert rows == []
    assert storage._objects == {}

    with pytest.raises(StorageKeyNotFound):
        await storage.open("uploads/does-not-matter.txt")


@pytest.mark.db
async def test_per_ticket_cap_rejects_the_next_upload(
    db_session: AsyncSession, session_factory: Any
) -> None:
    """Without a count cap, 'customers may upload' is an unbounded disk write."""
    principal, ticket_id = await _customer_with_ticket(
        db_session, session_factory, "int_att_cap@example.com"
    )
    storage = MemoryStorage()

    with patch("app.services.attachment_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.attachment_allowed_content_types = ("text/plain",)
        settings.attachment_max_bytes = 1024 * 1024
        settings.attachment_max_per_ticket = 2

        for index in range(2):
            async with session_factory() as session:
                service = AttachmentService(SqlAlchemyUnitOfWork(session), storage)
                await service.upload_attachment(principal, ticket_id, _upload(f"f{index}.txt"))

        async with session_factory() as session:
            service = AttachmentService(SqlAlchemyUnitOfWork(session), storage)
            with pytest.raises(BusinessRuleViolation, match="maximum of 2 attachments"):
                await service.upload_attachment(principal, ticket_id, _upload("f2.txt"))

    rows = (
        (await db_session.execute(select(Attachment).where(Attachment.ticket_id == ticket_id)))
        .scalars()
        .all()
    )
    assert len(rows) == 2
