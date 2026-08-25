"""lifecycle v2: SLA pause accounting and reopen

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-24 00:00:14.000000

The two-column design (spec05 §2):

``deadline`` stays the frozen original promise — written once at creation, never
recomputed. INV-1 and INV-2 survive verbatim rather than reinterpreted.

``sla_due_at`` is the current effective due time, materialized and recomputed at
exactly one moment: when the clock resumes. Between pauses it is a plain stored
timestamp, so it indexes like any other and the monitor's query shape does not
change. Computing the effective deadline on read would have preserved the
invariants and destroyed ix_ticket_sla_scan — the candidate query would become a
scan over an expression involving now(), which is unindexable.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ticket", sa.Column("sla_due_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("ticket", sa.Column("sla_paused_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "ticket",
        sa.Column("sla_paused_seconds", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "ticket",
        sa.Column("reopen_count", sa.Integer(), nullable=False, server_default="0"),
    )

    # Nothing has paused yet, so the effective due time is the original promise.
    # Accurate, not approximate.
    op.execute("UPDATE ticket SET sla_due_at = deadline WHERE sla_due_at IS NULL")
    op.alter_column("ticket", "sla_due_at", nullable=False)

    op.create_check_constraint(
        "sla_paused_seconds_non_negative", "ticket", "sla_paused_seconds >= 0"
    )
    op.create_check_constraint("reopen_count_non_negative", "ticket", "reopen_count >= 0")

    # INV-14, enforced by the database rather than by convention.
    op.create_check_constraint(
        "paused_iff_pending_customer",
        "ticket",
        "(sla_paused_at IS NOT NULL) = (status = 'PENDING_CUSTOMER')",
    )

    # Redefined against sla_due_at. Left alone the old constraint would still
    # PASS -- a paused ticket breaches at sla_due_at, which is later than
    # deadline -- so it would go on silently checking the wrong column.
    op.drop_constraint("breached_only_when_past_deadline", "ticket", type_="check")
    op.create_check_constraint(
        "breached_only_when_past_due",
        "ticket",
        "sla_breached_at IS NULL OR sla_breached_at >= sla_due_at",
    )

    # The monitor's partial index moves to the materialized column.
    # PENDING_CUSTOMER is excluded from the predicate as well as the query: a
    # paused ticket is never a breach candidate, so it should not occupy the
    # index at all.
    op.execute("DROP INDEX IF EXISTS ix_ticket_sla_scan")
    op.execute(
        "CREATE INDEX ix_ticket_sla_scan ON ticket (sla_due_at) "
        "WHERE sla_breached_at IS NULL "
        "AND status NOT IN ('RESOLVED', 'CLOSED', 'PENDING_CUSTOMER')"
    )


def downgrade() -> None:
    """Refuse over v2-only state (IMPLEMENTATION_V2.md §3.5).

    A paused or reopened ticket has no representation in the v1 schema. Dropping
    the columns would silently discard the fact that a ticket's clock was
    stopped, and its deadline would jump backwards by however long it waited.
    """
    bind = op.get_bind()
    blocked = bind.execute(
        sa.text(
            "SELECT count(*) FROM ticket "
            "WHERE status = 'PENDING_CUSTOMER' OR reopen_count > 0 OR sla_paused_seconds > 0"
        )
    ).scalar_one()
    if blocked:
        raise RuntimeError(
            f"Cannot downgrade: {blocked} ticket(s) are paused, have been reopened, or have "
            "accrued paused time. That state has no v1 representation — resolve it deliberately."
        )

    op.execute("DROP INDEX IF EXISTS ix_ticket_sla_scan")
    op.execute(
        "CREATE INDEX ix_ticket_sla_scan ON ticket (deadline) "
        "WHERE sla_breached_at IS NULL AND status NOT IN ('RESOLVED', 'CLOSED')"
    )

    op.drop_constraint("breached_only_when_past_due", "ticket", type_="check")
    op.create_check_constraint(
        "breached_only_when_past_deadline",
        "ticket",
        "sla_breached_at IS NULL OR sla_breached_at >= deadline",
    )

    op.drop_constraint("paused_iff_pending_customer", "ticket", type_="check")
    op.drop_constraint("reopen_count_non_negative", "ticket", type_="check")
    op.drop_constraint("sla_paused_seconds_non_negative", "ticket", type_="check")

    op.drop_column("ticket", "reopen_count")
    op.drop_column("ticket", "sla_paused_seconds")
    op.drop_column("ticket", "sla_paused_at")
    op.drop_column("ticket", "sla_due_at")
