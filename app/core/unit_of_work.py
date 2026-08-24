from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.attachment_repo import AttachmentRepository
from app.repositories.comment_repo import CommentRepository
from app.repositories.customer_repo import CustomerRepository
from app.repositories.event_repo import TicketEventRepository
from app.repositories.priority_rule_repo import PriorityRuleRepository
from app.repositories.ticket_repo import TicketRepository
from app.repositories.user_repo import UserRepository


class SqlAlchemyUnitOfWork:
    """
    The singular transaction boundary for the application.
    Orchestrates repositories and transaction commit/rollback.
    """

    def __init__(self, session: AsyncSession):
        self._session = session
        self.customers = CustomerRepository(session)
        self.priority_rules = PriorityRuleRepository(session)
        self.events = TicketEventRepository(session)
        self.tickets = TicketRepository(session)
        self.users = UserRepository(session)
        self.comments = CommentRepository(session)
        self.attachments = AttachmentRepository(session)

    async def __aenter__(self) -> SqlAlchemyUnitOfWork:
        # FastAPI's session dependency creates a session per request.
        # We start a transaction block here if it's not already started.
        # AsyncSession handles this implicitly when you do updates, but we can be explicit.
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if exc_type is not None:
            await self._session.rollback()
        else:
            await self._session.commit()
