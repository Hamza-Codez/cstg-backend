import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import CursorResult, Row, func, literal, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from app.core.clock import now
from app.core.pagination import Cursor
from app.domain import sla
from app.models.customer import Customer
from app.models.enums import TicketStatus
from app.models.ticket import Ticket
from app.models.user import AppUser
from app.schemas.ticket_filters import TicketFilters


@dataclass(frozen=True)
class TransitionWrites:
    """Columns a transition sets alongside `status`, in one guarded UPDATE.

    Separate `clear_*` flags because None already means "leave this column
    alone" — a reopen must be able to write NULL to resolved_at, which is a
    different intent from not touching it.
    """

    resolved_at: datetime | None = None
    clear_resolved_at: bool = False
    sla_paused_at: datetime | None = None
    clear_sla_paused_at: bool = False
    sla_paused_seconds: int | None = None
    sla_due_at: datetime | None = None
    increment_reopen: bool = False


def filter_predicates(filters: TicketFilters) -> list[ColumnElement[bool]]:
    """Every `GET /tickets` filter as a list of WHERE fragments.

    **Extracted, not duplicated** — the same reasoning as `ticket_scope`. The
    CSV export promises to return exactly the rows the list endpoint would
    (spec09 §6), and the only way to keep that promise through later filter
    changes is for both to build their WHERE clause from this function.
    """
    where: list[ColumnElement[bool]] = []

    if filters.q is not None:
        where.append(Ticket.search_vector.op("@@")(func.websearch_to_tsquery("english", filters.q)))
    if filters.status is not None:
        where.append(Ticket.status == filters.status)
    if filters.priority is not None:
        where.append(Ticket.priority == filters.priority)
    if filters.category is not None:
        where.append(Ticket.category == filters.category)
    if filters.assigned is not None:
        # Drives the dispatcher's Unassigned queue (UIUX_FRONTEND.md §7.3.1).
        where.append(
            Ticket.assignee_id.is_not(None) if filters.assigned else Ticket.assignee_id.is_(None)
        )
    if filters.breached is not None:
        where.append(
            Ticket.sla_breached_at.is_not(None)
            if filters.breached
            else Ticket.sla_breached_at.is_(None)
        )
    if filters.escalated is not None:
        where.append(
            Ticket.escalation_level > 0 if filters.escalated else Ticket.escalation_level == 0
        )
    if filters.assignee_id is not None:
        where.append(Ticket.assignee_id == filters.assignee_id)
    if filters.customer_id is not None:
        where.append(Ticket.customer_id == filters.customer_id)
    if filters.tier is not None:
        # The only filter needing a join; correlated so it composes with the
        # scope predicate rather than widening the row set.
        where.append(
            Ticket.customer_id.in_(select(Customer.id).where(Customer.tier == filters.tier))
        )
    # Half-open [after, before) so adjacent ranges neither overlap nor gap.
    if filters.created_after is not None:
        where.append(Ticket.created_at >= filters.created_after)
    if filters.created_before is not None:
        where.append(Ticket.created_at < filters.created_before)

    return where


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
        writes: "TransitionWrites | None" = None,
    ) -> bool:
        """Guarded conditional update: the only way a ticket changes status.

        Returns True if rowcount == 1, meaning the update applied and the state
        was as expected. False means a concurrency race or an illegal state, and
        the service raises 409.

        Every side effect of the transition — resolved_at, the pause columns,
        the reopen counter — is written **in this same statement**. If resume
        and the sla_due_at recomputation were two statements, a concurrent
        transition landing between them would leave the clock wrong with no
        guard to catch it (spec05 §7).
        """
        writes = writes or TransitionWrites()
        values: dict[str, object] = {"status": to_status, "updated_at": now()}

        if writes.resolved_at is not None:
            values["resolved_at"] = writes.resolved_at
        if writes.clear_resolved_at:
            # Required by resolved_requires_status: a reopened ticket is
            # IN_PROGRESS, which the CHECK does not allow to carry resolved_at.
            values["resolved_at"] = None

        if writes.sla_paused_at is not None:
            values["sla_paused_at"] = writes.sla_paused_at
        if writes.clear_sla_paused_at:
            values["sla_paused_at"] = None
        if writes.sla_paused_seconds is not None:
            values["sla_paused_seconds"] = writes.sla_paused_seconds
        if writes.sla_due_at is not None:
            values["sla_due_at"] = writes.sla_due_at
        if writes.increment_reopen:
            values["reopen_count"] = Ticket.reopen_count + 1

        stmt = (
            update(Ticket)
            .where(Ticket.id == ticket_id, Ticket.status == expected_from)
            .values(**values)
        )
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

    async def claim_if_unassigned(self, ticket_id: uuid.UUID, assignee_id: uuid.UUID) -> bool:
        """Guarded self-assignment (spec07 §4).

        `assignee_id IS NULL` is what makes claiming *taking* rather than
        reassigning: two agents scanning the same queue race, one wins, and the
        loser gets a 409 that says someone beat them to it.
        """
        stmt = (
            update(Ticket)
            .where(
                Ticket.id == ticket_id,
                Ticket.assignee_id.is_(None),
                Ticket.status.in_([TicketStatus.OPEN, TicketStatus.IN_PROGRESS]),
            )
            .values(assignee_id=assignee_id, updated_at=now())
        )
        result = await self.session.execute(stmt)
        assert isinstance(result, CursorResult)
        return result.rowcount == 1

    async def get_breach_candidates(self, current_time: datetime) -> Sequence[Ticket]:
        """Tickets past their effective due time and not yet escalated.

        Reads `sla_due_at`, not `deadline` — a paused ticket got that time back.
        The predicate mirrors `ix_ticket_sla_scan` exactly, including the
        PENDING_CUSTOMER exclusion; if the two drift, the index stops serving
        the query and paused tickets start escalating (spec05 §11).
        """
        stmt = select(Ticket).where(
            Ticket.sla_breached_at.is_(None),
            Ticket.status.notin_(sla.NON_BREACHING),
            Ticket.sla_due_at < current_time,
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
                # Idempotency guard (INV-6) and concurrency guard (INV-7).
                # PENDING_CUSTOMER joins the terminal statuses: a human pausing
                # the clock a millisecond earlier wins the race.
                Ticket.sla_breached_at.is_(None),
                Ticket.status.notin_(sla.NON_BREACHING),
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
            stmt = select(Ticket, rank)
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

        stmt = stmt.where(*filter_predicates(filters))

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
                        literal(cursor.row_id),
                    )
                )
            else:
                # Literal-wrapped so the comparison is a row-value expression,
                # which PostgreSQL can satisfy from the (created_at, id) ordering.
                stmt = stmt.where(
                    tuple_(Ticket.created_at, Ticket.id)
                    < tuple_(literal(cursor.created_at), literal(cursor.row_id))
                )

        stmt = stmt.limit(limit)
        rows = (await self.session.execute(stmt)).all()
        return [(row[0], row[1]) for row in rows]

    async def count_matching(
        self,
        *,
        scope: ColumnElement[bool],
        filters: TicketFilters | None = None,
    ) -> int:
        """How many rows an export would produce, asked before producing any.

        Streaming means the response has already started by the time the row
        cap would be hit, and a truncated file that does not say it is truncated
        is a wrong answer (spec09 §6). So the cap is enforced up front, with a
        COUNT the same predicates drive.
        """
        stmt = (
            select(func.count(Ticket.id))
            .where(scope)
            .where(*filter_predicates(filters or TicketFilters()))
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def stream_for_export(
        self,
        *,
        scope: ColumnElement[bool],
        filters: TicketFilters | None = None,
        chunk_size: int = 1000,
    ) -> AsyncIterator[Row[Any]]:
        """Server-side cursor over the export columns.

        `yield_per` keeps PostgreSQL streaming rather than buffering the whole
        result: this process also hosts the SLA monitor, and materialising 100k
        rows would stall breach detection (spec09 §6).

        **No body, no comments.** The export is operational data; bodies carry
        whatever a customer pasted into them.
        """
        assignee = aliased(AppUser)
        stmt = (
            select(
                Ticket.id,
                Customer.name.label("customer_name"),
                Customer.tier,
                Ticket.subject,
                Ticket.category,
                Ticket.priority,
                Ticket.status,
                assignee.name.label("assignee_name"),
                Ticket.created_at,
                Ticket.deadline,
                Ticket.sla_due_at,
                Ticket.sla_paused_seconds,
                Ticket.resolved_at,
                Ticket.sla_breached_at,
                Ticket.escalation_level,
                Ticket.reopen_count,
            )
            .join(Customer, Customer.id == Ticket.customer_id)
            .outerjoin(assignee, assignee.id == Ticket.assignee_id)
            .where(scope)
            .where(*filter_predicates(filters or TicketFilters()))
            .order_by(Ticket.created_at.desc(), Ticket.id.desc())
            .execution_options(yield_per=chunk_size)
        )
        result = await self.session.stream(stmt)
        async for row in result:
            yield row
