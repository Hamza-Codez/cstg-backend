import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, literal, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.clock import now
from app.core.pagination import Cursor
from app.models.customer import Customer
from app.models.enums import TicketStatus
from app.models.ticket import Ticket
from app.schemas.ticket_filters import TicketFilters


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
        filters: TicketFilters | None = None,
        limit: int = 50,
        cursor: Cursor | None = None,
    ) -> Sequence[tuple[Ticket, float | None]]:
        """Role-scoped, filtered, keyset-paginated ticket list (docs/API.md §4).

        Returns `(ticket, rank)` pairs; rank is None unless searching.

        **Scope first, then rank** (spec04 §3). The role scope and the full-text
        predicate are applied in the *same* statement, so there is no code path
        that ranks globally and filters afterwards. That ordering is the whole
        of INV-9 for search: relevance ranking naturally wants to run first, and
        doing so would leak the existence of other customers' tickets through
        result counts.

        Ordering is `(created_at DESC, id DESC)`, or
        `(rank DESC, created_at DESC, id DESC)` when searching. Including `id`
        makes the sort total: two tickets created in the same microsecond would
        otherwise have an unstable order and a cursor could skip or repeat one.

        Returns at most `limit` rows; the caller asks for `limit + 1` to learn
        whether another page exists.
        """
        filters = filters or TicketFilters()

        rank: ColumnElement[float] | None = None
        if filters.q is not None:
            query = func.websearch_to_tsquery("english", filters.q)
            rank = func.ts_rank_cd(Ticket.search_vector, query).label("rank")
            stmt = select(Ticket, rank).where(Ticket.search_vector.op("@@")(query))
            stmt = stmt.order_by(rank.desc(), Ticket.created_at.desc(), Ticket.id.desc())
        else:
            stmt = select(Ticket, literal(None)).order_by(
                Ticket.created_at.desc(), Ticket.id.desc()
            )

        # ── Role scope. Applied to the same statement as the search predicate. ──
        if customer_id is not None:
            stmt = stmt.where(Ticket.customer_id == customer_id)
        elif assignee_id is not None:
            stmt = stmt.where(Ticket.assignee_id == assignee_id)

        # ── Filters ────────────────────────────────────────────────────────────
        if filters.status is not None:
            stmt = stmt.where(Ticket.status == filters.status)
        if filters.priority is not None:
            stmt = stmt.where(Ticket.priority == filters.priority)
        if filters.category is not None:
            stmt = stmt.where(Ticket.category == filters.category)
        if filters.assigned is not None:
            # Drives the dispatcher's Unassigned queue (UIUX_FRONTEND.md §7.3.1).
            stmt = stmt.where(
                Ticket.assignee_id.is_not(None)
                if filters.assigned
                else Ticket.assignee_id.is_(None)
            )
        if filters.breached is not None:
            stmt = stmt.where(
                Ticket.sla_breached_at.is_not(None)
                if filters.breached
                else Ticket.sla_breached_at.is_(None)
            )
        if filters.escalated is not None:
            stmt = stmt.where(
                Ticket.escalation_level > 0 if filters.escalated else Ticket.escalation_level == 0
            )
        if filters.assignee_id is not None:
            stmt = stmt.where(Ticket.assignee_id == filters.assignee_id)
        if filters.customer_id is not None:
            stmt = stmt.where(Ticket.customer_id == filters.customer_id)
        if filters.tier is not None:
            # The only filter needing a join; correlated so it composes with the
            # scope predicate rather than widening the row set.
            stmt = stmt.where(
                Ticket.customer_id.in_(select(Customer.id).where(Customer.tier == filters.tier))
            )
        # Half-open [after, before) so adjacent ranges neither overlap nor gap.
        if filters.created_after is not None:
            stmt = stmt.where(Ticket.created_at >= filters.created_after)
        if filters.created_before is not None:
            stmt = stmt.where(Ticket.created_at < filters.created_before)

        # ── Keyset ─────────────────────────────────────────────────────────────
        if cursor is not None:
            if rank is not None and cursor.rank is not None:
                # Row-value comparison over the full sort key, so relevance ties
                # fall through to (created_at, id) exactly as the ORDER BY does.
                stmt = stmt.where(
                    tuple_(rank, Ticket.created_at, Ticket.id)
                    < tuple_(
                        literal(cursor.rank),
                        literal(cursor.created_at),
                        literal(cursor.ticket_id),
                    )
                )
            else:
                # Literal-wrapped so the comparison is a row-value expression,
                # which PostgreSQL can satisfy from the (created_at, id) ordering.
                stmt = stmt.where(
                    tuple_(Ticket.created_at, Ticket.id)
                    < tuple_(literal(cursor.created_at), literal(cursor.ticket_id))
                )

        stmt = stmt.limit(limit)
        rows = (await self.session.execute(stmt)).all()
        return [(row[0], row[1]) for row in rows]
