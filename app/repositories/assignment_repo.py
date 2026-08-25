"""Data access for assignment automation (spec07 §3)."""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.assignment_config import AssignmentConfig
from app.models.enums import EventType, Role, TicketStatus
from app.models.ticket import Ticket
from app.models.ticket_event import TicketEvent
from app.models.user import AppUser

#: An agent's queue. PENDING_CUSTOMER counts — a ticket waiting on a customer
#: still belongs to them and still occupies a slot, even though its clock is
#: stopped. Mirrors the predicate of ix_ticket_open_load exactly; if the two
#: drift, the count stops using the index.
_OPEN_STATUSES = (
    TicketStatus.OPEN,
    TicketStatus.IN_PROGRESS,
    TicketStatus.PENDING_CUSTOMER,
)


class AssignmentRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def config(self) -> AssignmentConfig | None:
        return (await self.session.execute(select(AssignmentConfig))).scalar_one_or_none()

    async def eligible_agents(self, *, for_automation: bool) -> Sequence[AppUser]:
        """Agents automation (or a dispatcher) may pick.

        INV-16 by construction: the predicate admits only active AGENTs, so a
        Candidate can never represent anyone else. `accepts_auto_assignment` is
        applied only for automation — a dispatcher assigning by hand has already
        made the decision that flag exists to defer.
        """
        stmt = select(AppUser).where(AppUser.role == Role.AGENT, AppUser.is_active.is_(True))
        if for_automation:
            stmt = stmt.where(AppUser.accepts_auto_assignment.is_(True))
        return (await self.session.execute(stmt)).scalars().all()

    async def open_counts(self) -> dict[uuid.UUID, int]:
        """Open tickets per agent, served by ix_ticket_open_load."""
        stmt = (
            select(Ticket.assignee_id, func.count())
            .where(Ticket.assignee_id.is_not(None), Ticket.status.in_(_OPEN_STATUSES))
            .group_by(Ticket.assignee_id)
        )
        rows = (await self.session.execute(stmt)).all()
        return {row[0]: row[1] for row in rows if row[0] is not None}

    async def last_assigned_at(self) -> dict[uuid.UUID, datetime]:
        """When each agent was last assigned anything.

        Derived from the ASSIGNMENT events rather than a counter column: a
        counter is a second source of truth that can drift from the audit log
        and needs its own concurrency story. The events are already append-only
        and already correct (spec07 §3).
        """
        assignee = TicketEvent.detail["assignee_id"].astext
        stmt = (
            select(assignee, func.max(TicketEvent.created_at))
            .where(TicketEvent.type == EventType.ASSIGNMENT, assignee.is_not(None))
            .group_by(assignee)
        )
        rows = (await self.session.execute(stmt)).all()
        return {uuid.UUID(row[0]): row[1] for row in rows if row[0]}

    async def open_count_for(self, agent_id: uuid.UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(Ticket)
            .where(Ticket.assignee_id == agent_id, Ticket.status.in_(_OPEN_STATUSES))
        )
        return int((await self.session.execute(stmt)).scalar_one())
