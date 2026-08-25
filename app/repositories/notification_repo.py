"""The notification read model (spec08 §5).

No writes on any state-change path. The only write is one cursor row per
principal, updated when they mark things read.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import Row, Select, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer import Customer
from app.models.enums import ActorType, EventType, TicketStatus
from app.models.notification_cursor import NotificationCursor
from app.models.ticket import Ticket
from app.models.ticket_event import TicketEvent
from app.models.user import AppUser
from app.repositories.ticket_scope import TicketScope

#: The badge never counts higher than this. "99+" is exactly as useful as 4,213
#: and costs a bounded query instead of an unbounded COUNT over the audit log.
COUNT_CAP = 100


class NotificationRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def cursor_for(
        self, principal_type: ActorType, principal_id: uuid.UUID
    ) -> NotificationCursor | None:
        stmt = select(NotificationCursor).where(
            NotificationCursor.principal_type == principal_type,
            NotificationCursor.principal_id == principal_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def account_created_at(
        self, principal_type: ActorType, principal_id: uuid.UUID
    ) -> datetime | None:
        """When this principal's account was created.

        A missing cursor means "never looked", and the floor is the account's
        own creation — not the beginning of time. Otherwise a new user's first
        visit shows a badge counting the entire history of the system.
        """
        model = Customer if principal_type is ActorType.CUSTOMER else AppUser
        stmt = select(model.created_at).where(model.id == principal_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    def _visible(
        self,
        scope: TicketScope,
        principal_id: uuid.UUID,
        since: datetime,
        visible_types: Sequence[EventType],
        *,
        public_replies_only: bool,
    ) -> Select[tuple[TicketEvent, str, TicketStatus]]:
        """The one query both the list and the count are built from."""
        stmt = (
            select(TicketEvent, Ticket.subject, Ticket.status)
            .join(Ticket, Ticket.id == TicketEvent.ticket_id)
            .where(
                scope.predicate(),
                TicketEvent.created_at > since,
                # IS DISTINCT FROM, never `<>`: actor_id is NULL for SYSTEM
                # events, and `<>` against NULL is NULL, which would silently
                # drop every SLA breach notification. That is the kind of bug
                # that ships quietly and is only noticed when someone asks why
                # breaches never notify (spec08 §5).
                TicketEvent.actor_id.is_distinct_from(principal_id),
                TicketEvent.type.in_(visible_types),
            )
        )
        if public_replies_only:
            # A customer is notified about a COMMENT only when it is a public
            # reply. An internal note must not produce a notification at all —
            # not even a contentless one, because a badge appearing when a note
            # is written leaks its existence and timing (INV-9).
            stmt = stmt.where(
                (TicketEvent.type != EventType.COMMENT)
                | (TicketEvent.detail["type"].astext == "PUBLIC_REPLY")
            )
        return stmt

    async def unread(
        self,
        scope: TicketScope,
        principal_id: uuid.UUID,
        since: datetime,
        visible_types: Sequence[EventType],
        *,
        public_replies_only: bool,
        limit: int,
    ) -> Sequence[Row[tuple[TicketEvent, str, TicketStatus]]]:
        stmt = self._visible(
            scope, principal_id, since, visible_types, public_replies_only=public_replies_only
        )
        stmt = stmt.order_by(TicketEvent.created_at.desc()).limit(limit)
        return (await self.session.execute(stmt)).all()

    async def unread_count(
        self,
        scope: TicketScope,
        principal_id: uuid.UUID,
        since: datetime,
        visible_types: Sequence[EventType],
        *,
        public_replies_only: bool,
    ) -> int:
        """Bounded: counts a capped subquery rather than the whole audit log."""
        inner = self._visible(
            scope, principal_id, since, visible_types, public_replies_only=public_replies_only
        ).limit(COUNT_CAP)
        stmt = select(func.count()).select_from(inner.subquery())
        return int((await self.session.execute(stmt)).scalar_one())

    async def actor_names(self, actor_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str]:
        """Names for the actors in a page, in two queries rather than N+1."""
        if not actor_ids:
            return {}
        names: dict[uuid.UUID, str] = {}
        for model in (AppUser, Customer):
            rows = (
                await self.session.execute(
                    select(model.id, model.name).where(model.id.in_(actor_ids))
                )
            ).all()
            names.update({row[0]: row[1] for row in rows})
        return names

    async def mark_read(
        self, principal_type: ActorType, principal_id: uuid.UUID, up_to: datetime
    ) -> None:
        """Upsert the cursor, moving it **forward only**.

        A backwards cursor would let a client resurrect old notifications
        indefinitely; "mark unread" is not a feature here.
        """
        stmt = (
            insert(NotificationCursor)
            .values(
                principal_type=principal_type,
                principal_id=principal_id,
                last_read_at=up_to,
            )
            .on_conflict_do_update(
                index_elements=["principal_type", "principal_id"],
                set_={"last_read_at": func.greatest(NotificationCursor.last_read_at, up_to)},
            )
        )
        await self.session.execute(stmt)
