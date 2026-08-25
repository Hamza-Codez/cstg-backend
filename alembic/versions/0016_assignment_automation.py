"""assignment automation: capacity, opt-out, and strategy

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-25 00:00:16.000000

Ships OFF (spec07 §2): `assignment_config` is seeded MANUAL with
auto_assign_on_create = false, so behaviour after this migration is identical to
before it. Automation is opt-in.

`max_open_tickets IS NULL` means no ceiling — the v1 behaviour — so existing
staff are unaffected.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("app_user", sa.Column("max_open_tickets", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "max_open_tickets_positive",
        "app_user",
        "max_open_tickets IS NULL OR max_open_tickets > 0",
    )
    # Lets an agent stay assignable by a dispatcher while being skipped by
    # automation — part-time, another rotation, on leave.
    op.add_column(
        "app_user",
        sa.Column(
            "accepts_auto_assignment",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )

    # Singleton: a boolean primary key with CHECK (id) admits exactly one row.
    # Chosen over a settings blob so the columns get real types and real
    # constraints, and so no code path can create a second config.
    op.create_table(
        "assignment_config",
        sa.Column("id", sa.Boolean(), primary_key=True, server_default=sa.text("true")),
        sa.Column("strategy", sa.String(length=20), nullable=False, server_default="MANUAL"),
        sa.Column(
            "auto_assign_on_create",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("updated_by", sa.Uuid(), sa.ForeignKey("app_user.id"), nullable=True),
        sa.CheckConstraint("id", name="assignment_config_singleton"),
        sa.CheckConstraint(
            "strategy IN ('MANUAL', 'ROUND_ROBIN', 'LEAST_LOADED')",
            name="assignment_config_strategy_valid",
        ),
    )
    op.execute("INSERT INTO assignment_config (id) VALUES (true)")

    # Serves the per-agent load count. PENDING_CUSTOMER is included on purpose:
    # a ticket waiting on a customer still belongs to its agent and still
    # occupies a slot, even though its clock is stopped (spec07 §2).
    op.execute(
        "CREATE INDEX ix_ticket_open_load ON ticket (assignee_id) "
        "WHERE status IN ('OPEN', 'IN_PROGRESS', 'PENDING_CUSTOMER')"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_ticket_open_load")
    op.drop_table("assignment_config")
    op.drop_column("app_user", "accepts_auto_assignment")
    op.drop_constraint("max_open_tickets_positive", "app_user", type_="check")
    op.drop_column("app_user", "max_open_tickets")
