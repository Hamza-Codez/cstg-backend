"""Notifications (spec08).

> Unread = the events on tickets I can currently read, created after my cursor,
> of a type I am allowed to see, that I did not cause.

**INV-17**: a notification never reveals an event on a ticket its recipient may
not read. This is a second read path over `ticket_event`, and a second read path
is a second chance to leak — so it reuses the *exact* scope predicate from
ticket listing rather than building its own idea of visibility.
"""

from datetime import datetime

from app.core.authorization import Principal
from app.core.clock import now
from app.core.unit_of_work import SqlAlchemyUnitOfWork
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

    async def _since(self, principal: Principal) -> datetime:
        cursor = await self.uow.notifications.cursor_for(principal.type, principal.id)
        if cursor is not None:
            return cursor.last_read_at
        # Never looked: the floor is this account's creation, so a first visit
        # does not count the entire history of the system.
        created = await self.uow.notifications.account_created_at(principal.type, principal.id)
        return created or now()

    async def count(self, principal: Principal) -> int:
        customer = self._is_customer(principal)
        return await self.uow.notifications.unread_count(
            scope_for(principal),
            principal.id,
            await self._since(principal),
            _CUSTOMER_EVENTS if customer else _STAFF_EVENTS,
            public_replies_only=customer,
        )

    async def page(self, principal: Principal, limit: int = 20) -> NotificationPage:
        customer = self._is_customer(principal)
        since = await self._since(principal)
        scope = scope_for(principal)
        types = _CUSTOMER_EVENTS if customer else _STAFF_EVENTS

        rows = await self.uow.notifications.unread(
            scope, principal.id, since, types, public_replies_only=customer, limit=limit
        )
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
            )
            for event, subject, _status in rows
        ]

        return NotificationPage(
            items=items,
            unread_count=await self.uow.notifications.unread_count(
                scope, principal.id, since, types, public_replies_only=customer
            ),
            last_read_at=since,
        )

    async def mark_read(self, principal: Principal, up_to: datetime | None = None) -> int:
        moment = up_to or now()
        # Clamped forward: a client must not be able to resurrect old
        # notifications by sending a past timestamp.
        moment = min(moment, now())
        await self.uow.notifications.mark_read(principal.type, principal.id, moment)
        return 0
