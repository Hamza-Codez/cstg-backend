"""Shared test harness (docs/TESTING.md).

Provides the two things P0 owes later phases: a disposable PostgreSQL and a freezable
clock. Both are lazy — ``pytest tests/unit`` requests neither, so the pure-domain suite
runs with no Docker and no database.
"""

import os
import pathlib
import sys

if sys.platform == "win32":
    import asyncio

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.config import Settings
from app.core import clock as clock_module
from app.core.clock import FrozenClock
from app.database import create_session_factory
from app.main import create_app

# Arbitrary but fixed: a stable "now" keeps SLA boundary assertions readable.
TEST_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


# ── Database ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """A throwaway PostgreSQL 16 container, started once per session on first use."""
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as container:
        yield container.get_connection_url()


@pytest.fixture(scope="session")
def migrated_database(postgres_url: str) -> str:
    """Build the test schema with Alembic, exactly as production is built.

    Deliberately NOT `Base.metadata.create_all()`. The ORM metadata knows nothing
    about anything a migration adds by hand — the append-only RULES on
    ticket_event (INV-10) and the column defaults among them — so a create_all
    schema silently differs from the real one, and tests would pass against
    protections that do not exist.

    Run as a subprocess because alembic/env.py calls `asyncio.run()`, which cannot
    be nested inside the test event loop.
    """
    import subprocess

    env = {**os.environ, "APP_DATABASE_URL": postgres_url}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=pathlib.Path(__file__).resolve().parent.parent,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic upgrade failed: {result.stdout} {result.stderr}")
    return postgres_url


@pytest.fixture(scope="function")
async def engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


@pytest.fixture
async def db_session(
    session_factory: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> AsyncIterator[AsyncSession]:
    """A clean database per test.

    TRUNCATE rather than DELETE: ticket_event carries the append-only RULES from
    migration 0006, which turn a DELETE into a silent no-op (INV-10). TRUNCATE is
    not subject to rules, so it is the only way to reset the audit table — and it
    is faster than row-by-row deletion besides.
    """
    from app.models import Base

    tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))
        await _seed_active_sla_policy(conn)

    async with session_factory() as session:
        yield session


async def _seed_active_sla_policy(conn: AsyncConnection) -> None:
    """Restore the SLA policy the TRUNCATE just removed (P17).

    Migration 0015 seeds an active policy, but sla_policy_version is in the
    metadata and so gets truncated with everything else. Ticket creation reads
    the active policy and refuses without one — correctly, since a system with
    no policy is misconfigured — so every test that creates a ticket needs it
    back.

    Seeded here rather than per-test because "there is always an active policy"
    is a property of the system, not of any individual test.
    """
    from app.domain.sla import DEFAULT_DURATIONS

    version_id = (
        await conn.execute(
            text(
                "INSERT INTO sla_policy_version (activated_at, note) "
                "VALUES (now(), 'test harness default') RETURNING id"
            )
        )
    ).scalar_one()

    for priority, delta in DEFAULT_DURATIONS.items():
        await conn.execute(
            text(
                "INSERT INTO sla_policy_entry (version_id, priority, seconds) "
                "VALUES (:vid, :priority, :seconds)"
            ),
            {"vid": version_id, "priority": priority.value, "seconds": int(delta.total_seconds())},
        )


# ── Clock ────────────────────────────────────────────────────────────────────


@pytest.fixture
def frozen_clock() -> Iterator[FrozenClock]:
    """Freeze ``app.core.clock.now()``; advance with ``frozen_clock.set(...)``.

    Always restores the real clock, so a test that crosses an SLA boundary cannot
    leak its time into the next one.
    """
    clock = FrozenClock(TEST_NOW)
    clock_module.set_clock(clock)
    try:
        yield clock
    finally:
        clock_module.reset_clock()


# ── Application ──────────────────────────────────────────────────────────────


@pytest.fixture
def test_settings() -> Settings:
    return Settings(env="test")


@pytest.fixture
async def client(
    test_settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[AsyncClient]:
    """HTTP client against the ASGI app, no network and no lifespan.

    Injects the session_factory directly into app.state since lifespan is not run.
    """
    app = create_app(test_settings)
    app.state.session_factory = session_factory
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
