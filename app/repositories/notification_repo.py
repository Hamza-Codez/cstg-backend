"""The notification read model (spec08 §5, extended at P21).

No writes on any state-change path. The only writes are one cursor row per
principal and, since P21, one dismissal row per notification a principal hides
from their own feed.

**Visibility and unread are two different questions**, and P21 is where they
stop being the same one:

    visible = in scope
            AND type allowed for this audience
            AND actor_id IS DISTINCT FROM me
            AND created_at > GREATEST(account_created_at, cleared_before)
            AND NOT EXISTS (my dismissal for this event)

    unread  = visible AND created_at > last_read_at

The badge counts `unread`; the feed lists `visible` with a `read` flag per item.
Before P21 the feed *was* the unread list, so opening the bell emptied it — the
panel could not hold a history because reading and seeing were one thing.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import Row, Select, and_, delete, exists, func, literal, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import Cursor
from app.models.customer import Customer
from app.models.enums import ActorType, EventType, TicketStatus
from app.models.notification_cursor import NotificationCursor
from app.models.notification_dismissal import NotificationDismissal
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
        principal_type: ActorType,
        principal_id: uuid.UUID,
        floor: datetime,
        visible_types: Sequence[EventType],
        *,
        public_replies_only: bool,
    ) -> Select[tuple[TicketEvent, str, TicketStatus]]:
        """The one query the list, the count and the dismissal lookup share.

        Three callers, one definition — the same reasoning as `ticket_scope`.
        The dismissal endpoint in particular *must* resolve its event through
        this and nothing else, or it becomes an existence oracle for arbitrary
        event ids (INV-9, INV-17).
        """
        dismissed = exists().where(
            and_(
                NotificationDismissal.principal_type == principal_type,
                NotificationDismissal.principal_id == principal_id,
                NotificationDismissal.event_id == TicketEvent.id,
            )
        )

        stmt = (
            select(TicketEvent, Ticket.subject, Ticket.status)
            .join(Ticket, Ticket.id == TicketEvent.ticket_id)
            .where(
                scope.predicate(),
                TicketEvent.created_at > floor,
                # IS DISTINCT FROM, never `<>`: actor_id is NULL for SYSTEM
                # events, and `<>` against NULL is NULL, which would silently
                # drop every SLA breach notification. That is the kind of bug
                # that ships quietly and is only noticed when someone asks why
                # breaches never notify (spec08 §5).
                TicketEvent.actor_id.is_distinct_from(principal_id),
                TicketEvent.type.in_(visible_types),
                ~dismissed,
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

    async def page(
        self,
        scope: TicketScope,
        principal_type: ActorType,
        principal_id: uuid.UUID,
        floor: datetime,
        visible_types: Sequence[EventType],
        *,
        public_replies_only: bool,
        limit: int,
        cursor: Cursor | None = None,
    ) -> Sequence[Row[tuple[TicketEvent, str, TicketStatus]]]:
        """Recent notifications, read and unread, newest first.

        Ordering is `(created_at DESC, id DESC)`. Including `id` makes the sort
        total: two events written in the same transaction share a timestamp to
        the microsecond, and an unstable order would let a cursor skip or repeat
        one. Returns at most `limit` rows; the caller asks for `limit + 1` to
        learn whether another page exists.
        """
        stmt = self._visible(
            scope,
            principal_type,
            principal_id,
            floor,
            visible_types,
            public_replies_only=public_replies_only,
        ).order_by(TicketEvent.created_at.desc(), TicketEvent.id.desc())

        if cursor is not None:
            # Row-value comparison so PostgreSQL can satisfy it from the
            # (created_at, id) ordering, exactly as ticket listing does.
            stmt = stmt.where(
                tuple_(TicketEvent.created_at, TicketEvent.id)
                < tuple_(literal(cursor.created_at), literal(cursor.row_id))
            )

        return (await self.session.execute(stmt.limit(limit))).all()

    async def visible_event(
        self,
        scope: TicketScope,
        principal_type: ActorType,
        principal_id: uuid.UUID,
        floor: datetime,
        visible_types: Sequence[EventType],
        *,
        public_replies_only: bool,
        event_id: uuid.UUID,
    ) -> TicketEvent | None:
        """One event, but only if this principal may see it.

        **The oracle guard.** Dismissal takes an event id from the client, and
        answering "that is not yours" differently from "that does not exist"
        would let anyone probe for the existence of arbitrary events. Resolving
        through `_visible` means both answer None, and the caller turns that
        into 404.

        Deliberately does *not* filter on dismissal state beyond what `_visible`
        already does — re-dismissing an already-dismissed event is idempotent
        and must not 404, since a double-click would otherwise surface an error.
        """
        stmt = self._visible(
            scope,
            principal_type,
            principal_id,
            floor,
            visible_types,
            public_replies_only=public_replies_only,
        ).where(TicketEvent.id == event_id)
        row = (await self.session.execute(stmt)).first()
        return row[0] if row else None

    async def exists_for_principal(
        self, principal_type: ActorType, principal_id: uuid.UUID, event_id: uuid.UUID
    ) -> bool:
        """Whether this principal already dismissed this event.

        Lets the service answer a repeat dismissal with 204 rather than the 404
        `visible_event` would give once the row exists and hides it.
        """
        stmt = select(
            exists().where(
                and_(
                    NotificationDismissal.principal_type == principal_type,
                    NotificationDismissal.principal_id == principal_id,
                    NotificationDismissal.event_id == event_id,
                )
            )
        )
        return (await self.session.execute(stmt)).scalar_one() is True

    async def unread_count(
        self,
        scope: TicketScope,
        principal_type: ActorType,
        principal_id: uuid.UUID,
        floor: datetime,
        read_boundary: datetime,
        visible_types: Sequence[EventType],
        *,
        public_replies_only: bool,
    ) -> int:
        """Bounded: counts a capped subquery rather than the whole audit log.

        Two boundaries, not one. `floor` decides what still exists for this
        principal; `read_boundary` decides which of those they have not seen.
        A dismissed or cleared notification cannot be unread, because it is not
        visible at all.
        """
        inner = (
            self._visible(
                scope,
                principal_type,
                principal_id,
                floor,
                visible_types,
                public_replies_only=public_replies_only,
            )
            .where(TicketEvent.created_at > read_boundary)
            .limit(COUNT_CAP)
        )
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

    async def dismiss(
        self, principal_type: ActorType, principal_id: uuid.UUID, event_id: uuid.UUID
    ) -> None:
        """Hide one event from one principal's feed. Idempotent.

        `DO NOTHING` rather than an existence check first: a double-click is the
        expected case, not an error, and the conflict is the cheapest way to say
        so without a round trip.
        """
        stmt = (
            insert(NotificationDismissal)
            .values(
                principal_type=principal_type,
                principal_id=principal_id,
                event_id=event_id,
            )
            .on_conflict_do_nothing(index_elements=["principal_type", "principal_id", "event_id"])
        )
        await self.session.execute(stmt)

    async def clear_all(
        self, principal_type: ActorType, principal_id: uuid.UUID, before: datetime
    ) -> None:
        """Hide everything up to `before`, in one UPDATE, and prune.

        Not a bulk insert of dismissal rows: that is an unbounded write inside a
        request handler, in the process that also hosts the SLA monitor.

        Forward-only for the same reason `last_read_at` is — a client must not
        be able to un-clear by sending an earlier timestamp.

        The prune is what keeps `notification_dismissal` from growing without
        limit: every row at or below the new floor is now redundant, because the
        floor hides those events anyway.
        """
        stmt = (
            insert(NotificationCursor)
            .values(
                principal_type=principal_type,
                principal_id=principal_id,
                # A first-ever clear still needs a read boundary; clearing
                # implies having seen, so they land together.
                last_read_at=before,
                cleared_before=before,
            )
            .on_conflict_do_update(
                index_elements=["principal_type", "principal_id"],
                set_={
                    "last_read_at": func.greatest(NotificationCursor.last_read_at, before),
                    "cleared_before": func.greatest(
                        # COALESCE because NULL means "never cleared", and
                        # GREATEST(NULL, x) is NULL in PostgreSQL — which would
                        # silently undo the clear.
                        func.coalesce(NotificationCursor.cleared_before, before),
                        before,
                    ),
                },
            )
        )
        await self.session.execute(stmt)

        redundant = select(TicketEvent.id).where(TicketEvent.created_at <= before)
        await self.session.execute(
            delete(NotificationDismissal).where(
                NotificationDismissal.principal_type == principal_type,
                NotificationDismissal.principal_id == principal_id,
                NotificationDismissal.event_id.in_(redundant),
            )
        )
