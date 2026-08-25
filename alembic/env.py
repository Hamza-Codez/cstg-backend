"""Alembic environment — async engine, URL and metadata sourced from the app.

No migrations exist at P0; the schema lands in P1 (docs/DATABASE.md §7).
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy.engine import Connection

# P1: importing app.models here registers the tables on Base.metadata so
# autogenerate can see them.
import app.models  # noqa
from alembic import context
from app.config import get_settings
from app.database import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    return get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        # One transaction PER MIGRATION, not one for the whole upgrade.
        #
        # Without this, `alembic upgrade head` from base runs every revision in
        # a single transaction — and PostgreSQL forbids *using* an enum value in
        # the transaction that added it. Migration 0013 adds PENDING_CUSTOMER
        # and 0014 writes a CHECK naming it: correct as separate revisions,
        # still fatal if they share a transaction (UnsafeNewEnumValueUsageError).
        #
        # The cost is that a failed upgrade leaves earlier revisions applied
        # rather than rolling the whole batch back. That is the right trade:
        # each migration is independently reversible, and alembic_version
        # records exactly where it stopped.
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        # One transaction PER MIGRATION, not one for the whole upgrade.
        #
        # Without this, `alembic upgrade head` from base runs every revision in
        # a single transaction — and PostgreSQL forbids *using* an enum value in
        # the transaction that added it. Migration 0013 adds PENDING_CUSTOMER
        # and 0014 writes a CHECK naming it: correct as separate revisions,
        # still fatal if they share a transaction (UnsafeNewEnumValueUsageError).
        #
        # The cost is that a failed upgrade leaves earlier revisions applied
        # rather than rolling the whole batch back. That is the right trade:
        # each migration is independently reversible, and alembic_version
        # records exactly where it stopped.
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    from app.database import create_engine

    engine = create_engine(_database_url())
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_do_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
