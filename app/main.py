"""Application factory.

The lifespan owns process-wide resources: the async engine and session factory now,
and from P5 the asyncio SLA monitor task (docs/ARCHITECTURE.md §6).
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import health
from app.api.v1.router import router as v1_router
from app.config import Settings, get_settings
from app.database import create_engine, create_session_factory
from app.workers.sla_monitor import run_sla_monitor


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    engine = create_engine(settings.database_url)
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)

    # The SLA monitor shares this process by design (docs/ARCHITECTURE.md §6);
    # its cadence comes from config, never a hardcoded default.
    monitor_task = asyncio.create_task(
        run_sla_monitor(
            app.state.session_factory,
            scan_interval_seconds=settings.sla_scan_interval_seconds,
        )
    )
    try:
        yield
    finally:
        monitor_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await monitor_task
        await engine.dispose()


def _configure_logging() -> None:
    """Give application loggers a handler.

    Uvicorn configures only its own loggers, so without this every
    ``logging.getLogger(__name__)`` message in the app is discarded — including
    the SLA monitor's. That matters: the monitor is the one component that runs
    with no request behind it, so silence is its only failure signal.
    """
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(levelname)s:     [%(name)s] %(message)s",
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    _configure_logging()

    app = FastAPI(
        title="Customer Support Ticket & SLA Automation Engine",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = settings

    from fastapi.exceptions import RequestValidationError

    from app.api.errors import (
        domain_error_handler,
        internal_error_handler,
        validation_exception_handler,
    )
    from app.domain.errors import DomainError

    app.add_exception_handler(DomainError, domain_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, internal_error_handler)

    app.include_router(health.router)
    app.include_router(v1_router)

    return app


app = create_app()
