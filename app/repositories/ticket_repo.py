import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import literal, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import now
from app.models.enums import Priority, TicketStatus
from app.models.ticket import Ticket


class TicketRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    def insert(self, ticket: Ticket) -> None:
        self.session.add(ticket)

    async def get(self, ticket_id: uuid.UUID) -> Ticket | None:
        stmt = select(Ticket).where(Ticket.id == ticket_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def transition_if(
        self,
        ticket_id: uuid.UUID,
        expected_from: TicketStatus,
        to_status: TicketStatus,
        resolved_at: datetime | None = None,
    ) -> bool:
        """
        Guarded conditional update.
        Returns True if rowcount == 1, meaning the update succeeded and the state was as expected.
        Returns False if rowcount == 0, indicating a concurrency race or illegal state.
        """
        values = {"status": to_status, "updated_at": now()}
        if resolved_at is not None:
            values["resolved_at"] = resolved_at

        stmt = (
            update(Ticket)
            .where(Ticket.id == ticket_id, Ticket.status == expected_from)
            .values(**values)
        )
        from sqlalchemy import CursorResult

        result = await self.session.execute(stmt)
        assert isinstance(result, CursorResult)
        return result.rowcount == 1

    async def assign_if_open_or_in_progress(
        self, ticket_id: uuid.UUID, assignee_id: uuid.UUID
    ) -> bool:
        """
        Guarded conditional update to set assignee_id.
        Returns True if rowcount == 1 (ticket was OPEN or IN_PROGRESS).
        Returns False if rowcount == 0 (ticket was not found or was in a terminal state).
        """
        stmt = (
            update(Ticket)
            .where(
                Ticket.id == ticket_id,
                Ticket.status.in_([TicketStatus.OPEN, TicketStatus.IN_PROGRESS]),
            )
            .values(assignee_id=assignee_id, updated_at=now())
        )
        from sqlalchemy import CursorResult

        result = await self.session.execute(stmt)
        assert isinstance(result, CursorResult)
        return result.rowcount == 1

    async def get_breach_candidates(self, current_time: datetime) -> Sequence[Ticket]:
        """Fetch tickets that are breached but haven't been escalated yet."""
        stmt = select(Ticket).where(
            Ticket.sla_breached_at.is_(None),
            Ticket.status.notin_([TicketStatus.RESOLVED, TicketStatus.CLOSED]),
            Ticket.deadline < current_time,
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def escalate_if(self, ticket_id: uuid.UUID, current_time: datetime) -> int | None:
        """
        Idempotent and concurrency-safe escalation.
        Returns the new escalation_level if successful, None if skipped (race or terminal).
        """
        stmt = (
            update(Ticket)
            .where(
                Ticket.id == ticket_id,
                Ticket.sla_breached_at.is_(None),
                Ticket.status.notin_([TicketStatus.RESOLVED, TicketStatus.CLOSED]),
            )
            .values(
                sla_breached_at=current_time,
                escalation_level=Ticket.escalation_level + 1,
                updated_at=current_time,
            )
            .returning(Ticket.escalation_level)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_scoped(
        self,
        *,
        customer_id: uuid.UUID | None = None,
        assignee_id: uuid.UUID | None = None,
        status: TicketStatus | None = None,
        priority: Priority | None = None,
        breached: bool | None = None,
        assigned: bool | None = None,
        limit: int = 50,
        cursor: tuple[datetime, uuid.UUID] | None = None,
    ) -> Sequence[Ticket]:
        """Role-scoped, filtered, keyset-paginated ticket list (docs/API.md §4).

        Ordering is `(created_at DESC, id DESC)`. Including `id` makes the sort
        total: two tickets created in the same microsecond would otherwise have an
        unstable order and a cursor could skip or repeat one.

        Returns at most `limit` rows; the caller asks for `limit + 1` to learn
        whether another page exists.
        """
        stmt = select(Ticket).order_by(Ticket.created_at.desc(), Ticket.id.desc())

        if customer_id is not None:
            stmt = stmt.where(Ticket.customer_id == customer_id)
        elif assignee_id is not None:
            stmt = stmt.where(Ticket.assignee_id == assignee_id)

        if status is not None:
            stmt = stmt.where(Ticket.status == status)
        if priority is not None:
            stmt = stmt.where(Ticket.priority == priority)
        if assigned is not None:
            # Drives the dispatcher's Unassigned queue (UIUX_FRONTEND.md §7.3.1).
            stmt = stmt.where(
                Ticket.assignee_id.is_not(None) if assigned else Ticket.assignee_id.is_(None)
            )
        if breached is not None:
            stmt = stmt.where(
                Ticket.sla_breached_at.is_not(None)
                if breached
                else Ticket.sla_breached_at.is_(None)
            )

        if cursor is not None:
            cursor_created_at, cursor_id = cursor
            # Literal-wrapped so the comparison is a row-value expression,
            # which PostgreSQL can satisfy from the (created_at, id) ordering.
            stmt = stmt.where(
                tuple_(Ticket.created_at, Ticket.id)
                < tuple_(literal(cursor_created_at), literal(cursor_id))
            )

        stmt = stmt.limit(limit)
        return (await self.session.execute(stmt)).scalars().all()
