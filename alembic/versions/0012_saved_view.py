"""saved_view

A named filter combination owned by one staff user.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-24 00:00:12.000000

Design notes (spec04 §6):

Staff only — a customer has two states for their requests list, so a saved view
would be furniture.

`ON DELETE CASCADE` is safe here and deliberate: unlike tickets and audit rows, a
saved view has no historical value once its owner is gone. Staff are deactivated
rather than deleted (docs/API.md §10), so this is belt-and-braces, not a routine
path.

`filters` is validated against the same Pydantic model the list endpoint uses,
at write time AND at execution time — roles change, and a view saved as a
dispatcher must not keep granting dispatcher-only filters afterwards.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "saved_view",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "owner_id",
            sa.Uuid(),
            sa.ForeignKey("app_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("filters", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("owner_id", "name", name="uq_saved_view_owner_name"),
    )
    op.create_index("ix_saved_view_owner", "saved_view", ["owner_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_saved_view_owner", table_name="saved_view")
    op.drop_table("saved_view")
