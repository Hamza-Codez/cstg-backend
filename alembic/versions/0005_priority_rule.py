"""priority_rule

Revision ID: 0005_priority_rule
Revises: 0004_comment_event
Create Date: 2026-08-21 00:00:05.000000
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0005_priority_rule"
down_revision = "0004_comment_event"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "priority_rule",
        sa.Column(
            "tier",
            postgresql.ENUM(
                "ENTERPRISE", "BUSINESS", "FREE", name="customertier", create_type=False
            ),
            nullable=False,
        ),
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
        sa.PrimaryKeyConstraint("tier", "category"),
    )


def downgrade() -> None:
    op.drop_table("priority_rule")
