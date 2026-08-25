"""per-principal notification dismissal and clear-all

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-25 00:00:19.000000

`ticket_event` is append-only — migration 0006 installs PostgreSQL RULES that
turn UPDATE and DELETE into silent no-ops — so "delete this notification" can
never touch the event. It is the audit log, and one person's inbox tidying must
not erase a record the whole system depends on.

Dismissal therefore becomes per-principal state, in two pieces that do two
different jobs:

  * `notification_dismissal` — one row per individually dismissed event.
  * `notification_cursor.cleared_before` — a timestamp for clear-all.

Clear-all is a timestamp rather than a bulk insert deliberately. Writing one
dismissal row per visible event is an unbounded write inside a request handler,
in the process that also hosts the SLA monitor; a timestamp is one UPDATE. It
also gives a cheap retention rule: dismissals at or below `cleared_before` are
redundant, so they are pruned when the clear happens and the table only ever
holds what has been individually dismissed since.

No backfill. A NULL `cleared_before` means "never cleared", which is what every
existing row should mean.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "notification_cursor",
        # Nullable, no default: NULL is "never cleared", which is a different
        # statement from "cleared at the epoch" and reads correctly in the
        # GREATEST() the read model uses.
        sa.Column("cleared_before", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "notification_dismissal",
        sa.Column(
            "principal_type",
            # Reuses actortype so one vocabulary for "who" spans ticket_event,
            # the JWT claims, notification_cursor and this table. `create_type`
            # is a *dialect* kwarg — sa.Enum(create_type=False) still emits
            # CREATE TYPE, which is why this is postgresql.ENUM (see 0005).
            postgresql.ENUM("CUSTOMER", "USER", "SYSTEM", name="actortype", create_type=False),
            primary_key=True,
        ),
        # No foreign key: the column points into `customer` or `app_user`
        # depending on principal_type — the same polymorphism ticket_event.
        # actor_id and notification_cursor.principal_id already live with.
        sa.Column("principal_id", sa.Uuid(), primary_key=True),
        # This one *does* get a foreign key: it points at exactly one table, and
        # a dismissal of an event that does not exist is meaningless.
        sa.Column(
            "event_id",
            sa.Uuid(),
            sa.ForeignKey("ticket_event.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "dismissed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # The monitor does not dismiss its own notifications. Mirrors
        # cursor_principal_not_system.
        sa.CheckConstraint("principal_type <> 'SYSTEM'", name="dismissal_principal_not_system"),
    )

    # The feed probes this with NOT EXISTS on every read, always filtered to one
    # principal. The primary key leads with the same two columns, so this index
    # is for the pruning DELETE in clear-all, which has no event_id to narrow on.
    op.execute(
        "CREATE INDEX ix_dismissal_principal "
        "ON notification_dismissal (principal_type, principal_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_dismissal_principal")
    op.drop_table("notification_dismissal")
    op.drop_column("notification_cursor", "cleared_before")
