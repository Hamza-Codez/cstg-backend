"""comment and event

Revision ID: 0004_comment_event
Revises: 0003_ticket
Create Date: 2026-08-21 00:00:04.000000
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0004_comment_event"
down_revision = "0003_ticket"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "comment",
        sa.Column("ticket_id", sa.Uuid(), nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=False),
        sa.Column(
            "type",
            postgresql.ENUM("INTERNAL_NOTE", "PUBLIC_REPLY", name="commenttype", create_type=False),
            nullable=False,
        ),
        sa.Column("body", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["app_user.id"],
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["ticket.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_comment_ticket", "comment", ["ticket_id", "created_at"], unique=False)

    op.create_table(
        "ticket_event",
        sa.Column("ticket_id", sa.Uuid(), nullable=False),
        sa.Column(
            "type",
            postgresql.ENUM(
                "CREATED",
                "STATUS_CHANGE",
                "ASSIGNMENT",
                "COMMENT",
                "SLA_BREACH",
                name="eventtype",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "actor_type",
            postgresql.ENUM("CUSTOMER", "USER", "SYSTEM", name="actortype", create_type=False),
            nullable=False,
        ),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column(
            "from_status",
            postgresql.ENUM(
                "OPEN", "IN_PROGRESS", "RESOLVED", "CLOSED", name="ticketstatus", create_type=False
            ),
            nullable=True,
        ),
        sa.Column(
            "to_status",
            postgresql.ENUM(
                "OPEN", "IN_PROGRESS", "RESOLVED", "CLOSED", name="ticketstatus", create_type=False
            ),
            nullable=True,
        ),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "(actor_type = 'SYSTEM') = (actor_id IS NULL)", name="system_actor_has_no_id"
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["ticket.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_event_ticket", "ticket_event", ["ticket_id", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_event_ticket", table_name="ticket_event")
    op.drop_table("ticket_event")
    op.drop_index("ix_comment_ticket", table_name="comment")
    op.drop_table("comment")
