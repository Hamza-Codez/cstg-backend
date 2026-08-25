"""comment customer author

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-24 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add author_customer_id
    op.add_column(
        "comment", sa.Column("author_customer_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_comment_author_customer_id", "comment", "customer", ["author_customer_id"], ["id"]
    )

    # Make author_user_id nullable and rename
    op.alter_column("comment", "author_id", new_column_name="author_user_id", nullable=True)

    # Add CheckConstraints
    op.create_check_constraint(
        "comment_exactly_one_author",
        "comment",
        "(author_user_id IS NULL) <> (author_customer_id IS NULL)",
    )
    op.create_check_constraint(
        "comment_customer_public_only",
        "comment",
        "author_customer_id IS NULL OR type = 'PUBLIC_REPLY'",
    )


def downgrade() -> None:
    # Fail if customer-authored rows exist (spec02 says it must not be silently discarded)
    conn = op.get_bind()
    res = conn.execute(
        sa.text("SELECT 1 FROM comment WHERE author_customer_id IS NOT NULL LIMIT 1")
    )
    if res.fetchone():
        raise ValueError("Cannot downgrade: customer-authored comments exist.")

    op.drop_constraint("comment_customer_public_only", "comment", type_="check")
    op.drop_constraint("comment_exactly_one_author", "comment", type_="check")

    op.alter_column("comment", "author_user_id", new_column_name="author_id", nullable=False)

    op.drop_constraint("fk_comment_author_customer_id", "comment", type_="foreignkey")
    op.drop_column("comment", "author_customer_id")
