from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ticket import Ticket
from app.models.ticket_event import TicketEvent
from tests.api.test_tickets import create_customer, seed_priority_rules


@pytest.mark.asyncio
@pytest.mark.db
async def test_ticket_creation_atomicity_on_event_failure(
    db_session: AsyncSession, session_factory: Any
) -> None:
    customer = await create_customer(db_session, "atomic@example.com")
    await seed_priority_rules(db_session)

    from app.core.authorization import Principal
    from app.core.unit_of_work import SqlAlchemyUnitOfWork
    from app.models.enums import ActorType, Category, Role
    from app.schemas.ticket import TicketCreate
    from app.services.ticket_service import TicketService

    principal = Principal(
        id=customer.id, type=ActorType.CUSTOMER, role=Role.CUSTOMER, is_active=True
    )

    # We mock TicketEvent to raise ValueError when instantiated
    with patch("app.services.ticket_service.TicketEvent") as mock_event:
        mock_event.side_effect = ValueError("Forced validation error")

        # Try to create a ticket and expect it to fail
        with pytest.raises(ValueError, match="Forced validation error"):
            async with session_factory() as service_session:
                service = TicketService(SqlAlchemyUnitOfWork(service_session))
                async with service.uow:
                    await service.create_ticket(
                        principal,
                        TicketCreate(
                            subject="Atomic Failure Test",
                            body="My issue",
                            category=Category.GENERAL,
                        ),
                    )

    # Verify that the transaction rolled back and no ticket was created
    tickets = (
        (await db_session.execute(select(Ticket).where(Ticket.subject == "Atomic Failure Test")))
        .scalars()
        .all()
    )
    assert len(tickets) == 0

    events = (await db_session.execute(select(TicketEvent))).scalars().all()
    assert len(events) == 0

    # Commit to avoid unhandled rollback in teardown which causes asyncpg loop crash
    await db_session.commit()
