import uuid
from datetime import datetime

from app.core.unit_of_work import SqlAlchemyUnitOfWork
from app.models.enums import ActorType, EventType
from app.models.ticket_event import TicketEvent


class SLAService:
    def __init__(self, uow: SqlAlchemyUnitOfWork):
        self.uow = uow

    async def escalate_due_breaches(self, current_time: datetime) -> int:
        """
        Scans for breached tickets and escalates them autonomously.
        Returns the number of tickets successfully escalated.
        """
        async with self.uow:
            candidates = await self.uow.tickets.get_breach_candidates(current_time)

        escalated_count = 0

        # We must process each candidate in its own transaction block
        # to ensure that one failure doesn't roll back successful escalations.
        for ticket in candidates:
            async with self.uow:
                new_level = await self.uow.tickets.escalate_if(ticket.id, current_time)

                # If new_level is None, it means the ticket was resolved/closed or
                # already escalated concurrently since the candidate fetch.
                if new_level is not None:
                    event = TicketEvent(
                        id=uuid.uuid4(),
                        ticket_id=ticket.id,
                        type=EventType.SLA_BREACH,
                        actor_type=ActorType.SYSTEM,
                        actor_id=None,
                        detail={"escalation_level": new_level},
                        created_at=current_time,
                    )
                    self.uow.events.insert(event)
                    escalated_count += 1

        return escalated_count
