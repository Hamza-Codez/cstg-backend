"""ticket

Revision ID: 0003_ticket
Revises: 0002_customer_user
Create Date: 2026-08-21 00:00:03.000000
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0003_ticket"
down_revision = "0002_customer_user"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ticket",
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("assignee_id", sa.Uuid(), nullable=True),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("body", sa.String(), nullable=False),
        sa.Column(
            "category",
            postgresql.ENUM(
                "OUTAGE", "BILLING", "TECHNICAL", "GENERAL", name="category", create_type=False
            ),
            nullable=False,
        ),
        sa.Column(
            "priority",
            postgresql.ENUM(
                "CRITICAL", "HIGH", "MEDIUM", "LOW", name="priority", create_type=False
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            postgresql.ENUM(
                "OPEN", "IN_PROGRESS", "RESOLVED", "CLOSED", name="ticketstatus", create_type=False
            ),
            nullable=False,
        ),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("escalation_level", sa.Integer(), nullable=False),
        sa.Column("sla_breached_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "resolved_at IS NULL OR status IN ('RESOLVED', 'CLOSED')",
            name="resolved_requires_status",
        ),
        sa.CheckConstraint("escalation_level >= 0", name="ticket_escalation_level_check"),
        sa.CheckConstraint(
            "sla_breached_at IS NULL OR sla_breached_at >= deadline",
            name="breached_only_when_past_deadline",
        ),
        sa.ForeignKeyConstraint(
            ["assignee_id"],
            ["app_user.id"],
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customer.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ticket_assignee_status", "ticket", ["assignee_id", "status"], unique=False)
    op.create_index(
        "ix_ticket_customer", "ticket", ["customer_id", sa.text("created_at DESC")], unique=False
    )
    op.create_index(
        "ix_ticket_sla_scan",
        "ticket",
        ["deadline"],
        unique=False,
        postgresql_where=sa.text(
            "sla_breached_at IS NULL AND status NOT IN ('RESOLVED', 'CLOSED')"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ticket_sla_scan",
        table_name="ticket",
        postgresql_where=sa.text(
            "sla_breached_at IS NULL AND status NOT IN ('RESOLVED', 'CLOSED')"
        ),
    )
    op.drop_index("ix_ticket_customer", table_name="ticket")
    op.drop_index("ix_ticket_assignee_status", table_name="ticket")
    op.drop_table("ticket")
