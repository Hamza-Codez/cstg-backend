"""Notifications (spec08, extended at P21).

> Visible = the events on tickets I can currently read, of a type I am allowed
> to see, that I did not cause, that I have not dismissed and that predate no
> clear-all of mine.
>
> Unread = the visible ones newer than my read cursor.

**INV-17**: a notification never reveals an event on a ticket its recipient may
not read. This is a second read path over `ticket_event`, and a second read path
is a second chance to leak — so it reuses the *exact* scope predicate from
ticket listing rather than building its own idea of visibility.

P21 adds a third way to get that wrong: `dismiss()` takes an event id straight
from the client. It resolves that id through the same visibility query as the
feed, so an event the caller may not see is indistinguishable from one that does
not exist — both 404. Anything looser turns the endpoint into an existence
oracle for arbitrary event ids.
"""

import uuid
from datetime import datetime

from app.core.authorization import Principal
from app.core.clock import now
from app.core.pagination import Cursor, encode_cursor
from app.core.unit_of_work import SqlAlchemyUnitOfWork
from app.domain.errors import NotFound
from app.models.enums import ActorType, EventType, Role
from app.repositories.ticket_scope import scope_for
from app.schemas.notification import NotificationItem, NotificationPage

#: Everything staff may be notified about.
_STAFF_EVENTS = (
    EventType.CREATED,
    EventType.STATUS_CHANGE,
    EventType.ASSIGNMENT,
    EventType.COMMENT,
    EventType.SLA_BREACH,
    EventType.ATTACHMENT,
)

#: Mirrors TicketService._CUSTOMER_VISIBLE_EVENTS. COMMENT is admitted here only
#: so the PUBLIC_REPLY filter can let replies through; internal notes are
#: excluded by that filter, never by omission.
_CUSTOMER_EVENTS = (
    EventType.CREATED,
    EventType.STATUS_CHANGE,
    EventType.SLA_BREACH,
    EventType.COMMENT,
)


class NotificationService:
    def __init__(self, uow: SqlAlchemyUnitOfWork):
        self.uow = uow

    @staticmethod
    def _is_customer(principal: Principal) -> bool:
        return principal.type is ActorType.CUSTOMER or principal.role is Role.CUSTOMER

    def _types(self, principal: Principal) -> tuple[EventType, ...]:
        return _CUSTOMER_EVENTS if self._is_customer(principal) else _STAFF_EVENTS

    async def _boundaries(self, principal: Principal) -> tuple[datetime, datetime]:
        """`(visibility_floor, read_boundary)` for this principal.

        Two values that used to be one. The floor decides what still exists in
        the feed at all — the account's creation, raised by any clear-all. The
        read boundary decides which of those the badge counts.

        Separating them is what lets the panel hold a history: before P21 the
        list filtered on the read cursor, so opening the bell emptied it.
        """
        cursor = await self.uow.notifications.cursor_for(principal.type, principal.id)
        # Never looked: the floor is this account's creation, so a first visit
        # does not show the entire history of the system.
        created = await self.uow.notifications.account_created_at(principal.type, principal.id)
        origin = created or now()

        if cursor is None:
            return origin, origin

        floor = max(origin, cursor.cleared_before) if cursor.cleared_before else origin
        return floor, cursor.last_read_at

    async def count(self, principal: Principal) -> int:
        customer = self._is_customer(principal)
        floor, read_boundary = await self._boundaries(principal)
        return await self.uow.notifications.unread_count(
            scope_for(principal),
            principal.type,
            principal.id,
            floor,
            read_boundary,
            self._types(principal),
            public_replies_only=customer,
        )

    async def page(
        self, principal: Principal, limit: int = 20, cursor: Cursor | None = None
    ) -> NotificationPage:
        customer = self._is_customer(principal)
        floor, read_boundary = await self._boundaries(principal)
        scope = scope_for(principal)
        types = self._types(principal)

        # One more than asked for, so a next page is detected rather than
        # guessed at from a full page.
        rows = await self.uow.notifications.page(
            scope,
            principal.type,
            principal.id,
            floor,
            types,
            public_replies_only=customer,
            limit=limit + 1,
            cursor=cursor,
        )
        has_more = len(rows) > limit
        rows = rows[:limit]

        names = await self.uow.notifications.actor_names(
            [row[0].actor_id for row in rows if row[0].actor_id is not None]
        )

        items = [
            NotificationItem(
                event_id=event.id,
                ticket_id=event.ticket_id,
                # Joined in so the client renders a line without an N+1. Both
                # are already visible to the recipient by construction: if the
                # ticket were not readable, the event would not be in the result.
                ticket_subject=subject,
                type=event.type,
                actor_type=event.actor_type,
                actor_name=names.get(event.actor_id) if event.actor_id else None,
                from_status=event.from_status,
                to_status=event.to_status,
                created_at=event.created_at,
                # Derived here rather than left to the client, which would have
                # to compare timestamps against a boundary it cannot trust its
                # own clock to interpret.
                read=event.created_at <= read_boundary,
            )
            for event, subject, _status in rows
        ]

        next_cursor = None
        if has_more and rows:
            last_event = rows[-1][0]
            next_cursor = encode_cursor(last_event.created_at, last_event.id)

        return NotificationPage(
            items=items,
            unread_count=await self.uow.notifications.unread_count(
                scope,
                principal.type,
                principal.id,
                floor,
                read_boundary,
                types,
                public_replies_only=customer,
            ),
            last_read_at=read_boundary,
            next_cursor=next_cursor,
        )

    async def mark_read(self, principal: Principal, up_to: datetime | None = None) -> int:
        moment = up_to or now()
        # Clamped forward: a client must not be able to resurrect old
        # notifications by sending a past timestamp.
        moment = min(moment, now())
        await self.uow.notifications.mark_read(principal.type, principal.id, moment)
        return 0

    async def dismiss(self, principal: Principal, event_id: uuid.UUID) -> None:
        """Hide one notification from this principal's feed alone.

        The audit event is untouched — it cannot be, and should not be. Only
        this principal's view of it changes.

        **404 for anything not visible**, including an event that simply does
        not exist. Distinguishing the two would let anyone enumerate event ids.
        """
        # Already dismissed is a success, not a 404. `_visible` excludes
        # dismissed events, so without this check a double-click — or a retry
        # after a dropped response — would surface an error for work that had
        # in fact succeeded.
        if await self.uow.notifications.exists_for_principal(
            principal.type, principal.id, event_id
        ):
            return

        customer = self._is_customer(principal)
        floor, _read_boundary = await self._boundaries(principal)

        event = await self.uow.notifications.visible_event(
            scope_for(principal),
            principal.type,
            principal.id,
            floor,
            self._types(principal),
            public_replies_only=customer,
            event_id=event_id,
        )
        if event is None:
            raise NotFound("That notification could not be found.")

        await self.uow.notifications.dismiss(principal.type, principal.id, event_id)

    async def clear_all(self, principal: Principal) -> None:
        """Empty this principal's feed up to now.

        A timestamp, not a row per notification: the latter is an unbounded
        write inside a request handler, in the process that also hosts the SLA
        monitor. Events created afterwards still arrive normally.
        """
        await self.uow.notifications.clear_all(principal.type, principal.id, now())
