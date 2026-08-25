"""pending customer status enum

Adds PENDING_CUSTOMER to ticket_status, and nothing else.

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-24 00:00:13.000000

Why this stands alone (spec05 §3):

PostgreSQL 16 permits ``ALTER TYPE ... ADD VALUE`` inside a transaction, but the
new value cannot be *used* in that same transaction — and Alembic wraps every
migration in one. Migration 0014 adds

    CHECK ((sla_paused_at IS NOT NULL) = (status = 'PENDING_CUSTOMER'))

which uses the literal. Combined into one migration that fails at runtime, not
at review, and not in a way an autogenerate diff would reveal.

The type is named ``ticketstatus`` (SQLAlchemy's default), not ``ticket_status``
as the sketch in docs/DATABASE.md §2 writes it. See 0001.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_V1_STATUSES = ("OPEN", "IN_PROGRESS", "RESOLVED", "CLOSED")


def upgrade() -> None:
    op.execute("ALTER TYPE ticketstatus ADD VALUE IF NOT EXISTS 'PENDING_CUSTOMER'")


def downgrade() -> None:
    """Rebuild ticketstatus without PENDING_CUSTOMER, refusing if any row uses it.

    PostgreSQL cannot drop an enum value in place. Refusing when the value is in
    use is the honest failure mode: a paused ticket has no representation in the
    v1 schema, and silently reassigning its status would lose the fact that work
    was waiting on the customer.
    """
    bind = op.get_bind()
    in_use = bind.execute(
        sa.text("SELECT count(*) FROM ticket WHERE status = 'PENDING_CUSTOMER'")
    ).scalar_one()
    if in_use:
        raise RuntimeError(
            f"Cannot downgrade: {in_use} ticket(s) are PENDING_CUSTOMER. "
            "Resolve or resume them before downgrading."
        )

    # ticket_event records history and may legitimately reference the value in
    # from_status/to_status, so those columns are rebuilt too.
    op.execute("ALTER TYPE ticketstatus RENAME TO ticketstatus_old")
    sa.Enum(*_V1_STATUSES, name="ticketstatus").create(bind)

    # Everything that references ticket.status has to come off first, or the
    # type change fails trying to compare the new type against the old:
    #   * DEFAULT 'OPEN' (migration 0007)
    #   * ix_ticket_sla_scan, whose predicate filters on status
    #   * resolved_requires_status, whose expression names two statuses
    # Each is restored below exactly as v1 defined it.
    op.execute("ALTER TABLE ticket ALTER COLUMN status DROP DEFAULT")
    op.execute("DROP INDEX IF EXISTS ix_ticket_sla_scan")
    op.execute("ALTER TABLE ticket DROP CONSTRAINT IF EXISTS resolved_requires_status")
    for table, column in (
        ("ticket", "status"),
        ("ticket_event", "from_status"),
        ("ticket_event", "to_status"),
    ):
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} TYPE ticketstatus "
            f"USING {column}::text::ticketstatus"
        )
    op.execute("ALTER TABLE ticket ALTER COLUMN status SET DEFAULT 'OPEN'")
    op.execute(
        "ALTER TABLE ticket ADD CONSTRAINT resolved_requires_status "
        "CHECK (resolved_at IS NULL OR status IN ('RESOLVED', 'CLOSED'))"
    )
    op.execute(
        "CREATE INDEX ix_ticket_sla_scan ON ticket (deadline) "
        "WHERE sla_breached_at IS NULL AND status NOT IN ('RESOLVED', 'CLOSED')"
    )
    op.execute("DROP TYPE ticketstatus_old")
