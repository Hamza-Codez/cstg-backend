# ruff: noqa: E501
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authorization import Principal
from app.models.enums import ActorType, CommentType, Role
from app.models.ticket_event import TicketEvent
from app.services.comment_service import CommentService
from tests.api.test_tickets import create_agent, create_customer


@pytest.mark.asyncio
@pytest.mark.db
async def test_comment_authorship_inv11(db_session: AsyncSession) -> None:
    """INV-11: Exactly one author column is set on every comment."""
    from app.core.unit_of_work import SqlAlchemyUnitOfWork
    uow = SqlAlchemyUnitOfWork(db_session)
    comment_service = CommentService(uow)

    customer = await create_customer(db_session, "inv11_c@example.com")
    agent = await create_agent(db_session, "inv11_a@example.com")

    # Seed a ticket via raw SQL to bypass full creation machinery
    customer_id = customer.id
    agent_id = agent.id

    ticket_id = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO ticket (id, customer_id, assignee_id, subject, body, category, priority, status, created_at, deadline) "
            "VALUES (:id, :cid, :aid, 'S', 'B', 'GENERAL', 'MEDIUM', 'OPEN', :now, :now)"
        ),
        {"id": ticket_id, "cid": customer_id, "aid": agent_id, "now": datetime.now(UTC)},
    )
    await db_session.commit()

    customer_principal = Principal(id=customer_id, type=ActorType.CUSTOMER, role=Role.CUSTOMER, is_active=True)
    agent_principal = Principal(id=agent_id, type=ActorType.USER, role=Role.AGENT, is_active=True)

    # Customer comment
    from app.schemas.comment import CommentCreate

    c1 = await comment_service.add_comment(
        customer_principal, ticket_id, CommentCreate(type=CommentType.PUBLIC_REPLY, body="c1")
    )

    # Reload and assert DB state
    await db_session.refresh(c1)
    assert c1.author_customer_id == customer_id
    assert c1.author_user_id is None

    # Agent comment
    c2 = await comment_service.add_comment(
        agent_principal, ticket_id, CommentCreate(type=CommentType.PUBLIC_REPLY, body="c2")
    )
    await db_session.refresh(c2)
    assert c2.author_customer_id is None
    assert c2.author_user_id == agent_id


@pytest.mark.asyncio
@pytest.mark.db
async def test_comment_check_constraints_at_db_level(db_session: AsyncSession) -> None:
    """Direct insert violating either CHECK is rejected by the database."""
    customer = await create_customer(db_session, "chk_c@example.com")
    agent = await create_agent(db_session, "chk_a@example.com")

    customer_id = customer.id
    agent_id = agent.id

    ticket_id = uuid.uuid4()
    now = datetime.now(UTC)
    await db_session.execute(
        text(
            "INSERT INTO ticket (id, customer_id, subject, body, category, priority, status, created_at, deadline) "
            "VALUES (:id, :cid, 'S', 'B', 'GENERAL', 'MEDIUM', 'OPEN', :now, :now)"
        ),
        {"id": ticket_id, "cid": customer_id, "now": now},
    )
    await db_session.commit()

    # Violate comment_exactly_one_author (both null)
    with pytest.raises(IntegrityError, match="comment_exactly_one_author"):
        await db_session.execute(
            text(
                "INSERT INTO comment (id, ticket_id, type, body, created_at) "
                "VALUES (:id, :tid, 'PUBLIC_REPLY', 'B', :now)"
            ),
            {"id": uuid.uuid4(), "tid": ticket_id, "now": now},
        )
    await db_session.rollback()

    # Violate comment_exactly_one_author (both set)
    with pytest.raises(IntegrityError, match="comment_exactly_one_author"):
        await db_session.execute(
            text(
                "INSERT INTO comment (id, ticket_id, author_customer_id, author_user_id, type, body, created_at) "
                "VALUES (:id, :tid, :cid, :uid, 'PUBLIC_REPLY', 'B', :now)"
            ),
            {"id": uuid.uuid4(), "tid": ticket_id, "cid": customer_id, "uid": agent_id, "now": now},
        )
    await db_session.rollback()

    # Violate comment_customer_public_only
    with pytest.raises(IntegrityError, match="comment_customer_public_only"):
        await db_session.execute(
            text(
                "INSERT INTO comment (id, ticket_id, author_customer_id, type, body, created_at) "
                "VALUES (:id, :tid, :cid, 'INTERNAL_NOTE', 'B', :now)"
            ),
            {"id": uuid.uuid4(), "tid": ticket_id, "cid": customer_id, "now": now},
        )
    await db_session.rollback()


@pytest.mark.asyncio
@pytest.mark.db
async def test_comment_atomicity_with_event(db_session: AsyncSession) -> None:
    """Comment + COMMENT event commit atomically; forced event-insert failure rolls back the comment."""
    from app.core.unit_of_work import SqlAlchemyUnitOfWork
    uow = SqlAlchemyUnitOfWork(db_session)
    comment_service = CommentService(uow)

    customer = await create_customer(db_session, "atom_c@example.com")
    customer_id = customer.id

    ticket_id = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO ticket (id, customer_id, subject, body, category, priority, status, created_at, deadline) "
            "VALUES (:id, :cid, 'S', 'B', 'GENERAL', 'MEDIUM', 'OPEN', :now, :now)"
        ),
        {"id": ticket_id, "cid": customer_id, "now": datetime.now(UTC)},
    )
    await db_session.commit()

    customer_principal = Principal(
        id=customer_id, type=ActorType.CUSTOMER, role=Role.CUSTOMER, is_active=True
    )

    # We mock the events repo to raise an error
    from app.schemas.comment import CommentCreate

    # Save original insert
    original_insert = comment_service.uow.events.insert

    def failing_insert(event: TicketEvent) -> None:
        raise ValueError("Simulated event failure")

    comment_service.uow.events.insert = failing_insert

    try:
        with pytest.raises(ValueError, match="Simulated event failure"):
            await comment_service.add_comment(
                customer_principal,
                ticket_id,
                CommentCreate(type=CommentType.PUBLIC_REPLY, body="fail"),
            )
    finally:
        comment_service.uow.events.insert = original_insert

    # Ensure no comment was inserted
    count = (
        await db_session.execute(
            select(text("COUNT(*) FROM comment WHERE ticket_id = :tid")).params(tid=ticket_id)
        )
    ).scalar_one()
    assert count == 0
