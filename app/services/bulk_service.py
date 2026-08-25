import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.core.authorization import Principal
from app.core.unit_of_work import SqlAlchemyUnitOfWork
from app.domain.errors import DomainError, ValidationError
from app.models.ticket import Ticket
from app.schemas.bulk import (
    BulkAssignmentRequest,
    BulkItemResult,
    BulkReassignmentRequest,
    BulkResult,
    BulkTransitionRequest,
)
from app.schemas.common import ErrorDetail
from app.services.assignment_service import AssignmentService
from app.services.ticket_service import TicketService

logger = logging.getLogger(__name__)


class BulkService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self.session_factory = session_factory

    def _map_error(self, exc: Exception) -> ErrorDetail:
        from app.api.errors import _STATUS_BY_ERROR

        if isinstance(exc, DomainError):
            code = "INTERNAL_ERROR"
            for error_type, _, mapped_code in _STATUS_BY_ERROR:
                if isinstance(exc, error_type):
                    code = mapped_code
                    break
            return ErrorDetail(code=code, message=str(exc) or "An error occurred", details={})

        # INV-18: One item's failure neither rolls back nor blocks the others.
        # This is the one place in the codebase where swallowing an exception is correct
        # behaviour rather than a smell. It must be commented as such.
        logger.exception("Unexpected error in bulk item processing")
        return ErrorDetail(
            code="INTERNAL_ERROR", message="An unexpected error occurred.", details={}
        )

    async def assign_tickets(self, principal: Principal, req: BulkAssignmentRequest) -> BulkResult:
        results = []
        for ticket_id in req.ticket_ids:
            try:
                # One session per item, still: the loop opens a fresh one
                # each pass, so one failure cannot roll back the rest (INV-18).
                async with (
                    self.session_factory() as session,
                    SqlAlchemyUnitOfWork(session) as uow,
                ):
                        service = AssignmentService(uow)
                        await service.assign_ticket(
                            principal,
                            ticket_id,
                            req.assignee_id,
                            override_capacity=req.override_capacity,
                        )
                results.append(BulkItemResult(ticket_id=ticket_id, ok=True))
            except Exception as e:
                results.append(
                    BulkItemResult(ticket_id=ticket_id, ok=False, error=self._map_error(e))
                )

        succeeded = sum(1 for r in results if r.ok)
        failed = len(results) - succeeded
        return BulkResult(
            requested=len(req.ticket_ids), succeeded=succeeded, failed=failed, results=results
        )

    async def transition_tickets(
        self, principal: Principal, req: BulkTransitionRequest
    ) -> BulkResult:
        results = []
        for ticket_id in req.ticket_ids:
            try:
                # One session per item, still: the loop opens a fresh one
                # each pass, so one failure cannot roll back the rest (INV-18).
                async with (
                    self.session_factory() as session,
                    SqlAlchemyUnitOfWork(session) as uow,
                ):
                        service = TicketService(uow)
                        await service.transition_ticket(principal, ticket_id, req.to)
                results.append(BulkItemResult(ticket_id=ticket_id, ok=True))
            except Exception as e:
                results.append(
                    BulkItemResult(ticket_id=ticket_id, ok=False, error=self._map_error(e))
                )

        succeeded = sum(1 for r in results if r.ok)
        failed = len(results) - succeeded
        return BulkResult(
            requested=len(req.ticket_ids), succeeded=succeeded, failed=failed, results=results
        )

    async def reassign_tickets(
        self, principal: Principal, req: BulkReassignmentRequest
    ) -> BulkResult:
        settings = get_settings()
        max_items = settings.bulk_max_items

        async with self.session_factory() as session:
            stmt = (
                select(Ticket.id)
                .where(
                    Ticket.assignee_id == req.from_assignee_id,
                    Ticket.status.in_(req.statuses),
                )
                .limit(max_items + 1)
            )
            rows = (await session.execute(stmt)).scalars().all()

        if len(rows) > max_items:
            raise ValidationError(
                f"That agent has more than {max_items} tickets. Narrow by status and try again."
            )

        if not rows:
            raise ValidationError("No tickets matched the criteria.")

        assign_req = BulkAssignmentRequest(
            ticket_ids=list(rows),
            assignee_id=req.to_assignee_id,
            override_capacity=False,
        )
        return await self.assign_tickets(principal, assign_req)
