"""metrics supporting indexes

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-25 00:00:18.000000

Metrics scan the whole ticket table by construction. These two partial indexes
serve the time-series buckets over resolved_at and sla_breached_at, which are
sparse: most tickets are neither resolved nor breached, so a partial index is a
fraction of the size of a full one.

**No materialized views and no caching.** Both are the documented answer if
these queries ever get slow at real volume, and ARCHITECTURE.md §6 sets the
posture: measure first, then act, and record the trigger. Adding a refresh
schedule now would introduce staleness and a second background task for a
problem that does not exist yet.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_ticket_resolved_at ON ticket (resolved_at) "
        "WHERE resolved_at IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX ix_ticket_breached_at ON ticket (sla_breached_at) "
        "WHERE sla_breached_at IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_ticket_breached_at")
    op.execute("DROP INDEX IF EXISTS ix_ticket_resolved_at")
