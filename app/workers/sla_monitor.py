import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import now
from app.core.unit_of_work import SqlAlchemyUnitOfWork
from app.services.sla_service import SLAService

logger = logging.getLogger(__name__)


async def run_sla_monitor(
    session_factory: async_sessionmaker[AsyncSession],
    scan_interval_seconds: float,
) -> None:
    """
    Background loop that wakes up periodically and executes the SLA monitor.
    Uses its own db sessions mapped from the injected session_factory.
    """
    logger.info(f"SLA monitor started (interval: {scan_interval_seconds}s)")

    try:
        while True:
            # We create a new session explicitly for each loop iteration
            # to prevent connection holding when idle.
            async with session_factory() as session:
                uow = SqlAlchemyUnitOfWork(session)
                sla_service = SLAService(uow)

                try:
                    escalated = await sla_service.escalate_due_breaches(now())
                    if escalated > 0:
                        logger.info(f"SLA monitor escalated {escalated} tickets.")
                except Exception as e:
                    logger.error(f"SLA monitor encountered an error: {e}")

            # Wait for next scan interval
            await asyncio.sleep(scan_interval_seconds)

    except asyncio.CancelledError:
        logger.info("SLA monitor cancelled and shutting down gracefully.")
        raise
