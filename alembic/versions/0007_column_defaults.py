"""column_defaults

Restores the server-side defaults specified in docs/DATABASE.md §3, which the
initial table migrations omitted. The application already supplies these values
explicitly, so nothing changes for existing code paths; the defaults matter for
direct inserts (seeds, migrations, operational fixes) and keep the physical schema
matching the spec that documents it.

Revision ID: 0007_column_defaults
Revises: 99de55862c0e
Create Date: 2026-08-21 00:00:07.000000
"""

from alembic import op

revision = "0007_column_defaults"
down_revision = "99de55862c0e"
branch_labels = None
depends_on = None

# (table, column, default) exactly as documented in DATABASE.md §3.
_DEFAULTS = [
    ("customer", "id", "gen_random_uuid()"),
    ("customer", "created_at", "now()"),
    ("app_user", "id", "gen_random_uuid()"),
    ("app_user", "created_at", "now()"),
    ("app_user", "is_active", "true"),
    ("ticket", "id", "gen_random_uuid()"),
    ("ticket", "status", "'OPEN'::ticketstatus"),
    ("ticket", "escalation_level", "0"),
    ("ticket", "created_at", "now()"),
    ("ticket", "updated_at", "now()"),
    ("comment", "id", "gen_random_uuid()"),
    ("comment", "created_at", "now()"),
    ("ticket_event", "id", "gen_random_uuid()"),
    ("ticket_event", "created_at", "now()"),
]


def upgrade() -> None:
    # pgcrypto supplies gen_random_uuid() on PostgreSQL < 13; harmless on 16.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    for table, column, default in _DEFAULTS:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT {default}")


def downgrade() -> None:
    for table, column, _ in _DEFAULTS:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} DROP DEFAULT")
