"""notification read cursor

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-25 00:00:17.000000

Notifications are a **read model** over ticket_event, not a table of rows
(spec08 §2). The obvious design writes one row per recipient per event, which
means fan-out writes inside every state-changing transaction — the transaction
that must stay fast because the SLA monitor shares this process — and recipient
sets computed at write time that go stale when assignment changes.

So the only thing stored is where each principal has read up to. Everything else
is derived at read time from current visibility, which is why an agent removed
from a ticket immediately stops seeing its notifications.

Table only, no backfill: a missing cursor means "never looked", and the read
model treats that as the principal's account creation rather than the entire
history of the system.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_cursor",
        sa.Column(
            "principal_type",
            # Reuses actor_type so one vocabulary for "who" spans ticket_event,
            # the JWT claims, and this table (AUTHORIZATION.md §1).
            postgresql.ENUM("CUSTOMER", "USER", "SYSTEM", name="actortype", create_type=False),
            primary_key=True,
        ),
        # No foreign key: the column points into `customer` or `app_user`
        # depending on principal_type — the same polymorphism ticket_event.
        # actor_id already lives with. Unlike a comment's author, this is derived
        # state that can be rebuilt from nothing, so an exclusive-arc pair would
        # earn nothing.
        sa.Column("principal_id", sa.Uuid(), primary_key=True),
        sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # The monitor does not read its own notifications.
        sa.CheckConstraint("principal_type <> 'SYSTEM'", name="cursor_principal_not_system"),
    )

    # Serves the unscoped dispatcher/admin feed, which has no ticket predicate
    # to narrow it.
    op.execute("CREATE INDEX ix_event_created_desc ON ticket_event (created_at DESC)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_event_created_desc")
    op.drop_table("notification_cursor")
