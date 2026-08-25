"""versioned SLA policy

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-25 00:00:15.000000

Why versions rather than a mutable table (spec06 §2):

Because `deadline` is frozen at creation (INV-1), changing durations is already
safe for existing tickets — they keep the terms they were created under. What
versioning adds is *explicability*: a ticket showing a six-hour window when the
config says eight is otherwise indistinguishable from a bug.

Versions and entries are never updated or deleted. Editing a policy creates a
new version, for the same reason ticket_event is append-only: a record that
explains a past decision has to still be there when someone asks.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The v1 durations, moving out of app/domain/sla.py and into data.
_SEED = {"CRITICAL": 2 * 3600, "HIGH": 8 * 3600, "MEDIUM": 24 * 3600, "LOW": 72 * 3600}


def upgrade() -> None:
    op.create_table(
        "sla_policy_version",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # NULL for the seeded v1 policy: no admin authored it.
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.String(length=200), nullable=True),
    )

    op.create_table(
        "sla_policy_entry",
        sa.Column(
            "version_id",
            sa.Uuid(),
            sa.ForeignKey("sla_policy_version.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "priority",
            # postgresql.ENUM, not sa.Enum: create_type=False is a dialect
            # kwarg, and sa.Enum emits CREATE TYPE regardless. The type
            # already exists from 0001. Matches 0005's pattern.
            postgresql.ENUM(
                "CRITICAL", "HIGH", "MEDIUM", "LOW", name="priority", create_type=False
            ),
            primary_key=True,
        ),
        sa.Column("seconds", sa.Integer(), nullable=False),
        sa.CheckConstraint("seconds > 0", name="sla_policy_seconds_positive"),
    )

    # Exactly one active policy, enforced by the database rather than hoped for
    # by the application. Two concurrent activations mean one transaction loses
    # on this index and returns 409 — the same "let the database arbitrate"
    # posture as the guarded conditional UPDATE.
    op.execute(
        "CREATE UNIQUE INDEX ix_sla_policy_active ON sla_policy_version "
        "((activated_at IS NOT NULL)) "
        "WHERE activated_at IS NOT NULL AND superseded_at IS NULL"
    )

    op.add_column(
        "ticket",
        sa.Column(
            "sla_policy_version_id",
            sa.Uuid(),
            sa.ForeignKey("sla_policy_version.id"),
            nullable=True,
        ),
    )

    # Seed v1 from the domain constants and pin every existing ticket to it.
    # Those are exactly the terms they were created under, so the backfill is
    # accurate rather than approximate.
    bind = op.get_bind()
    version_id = bind.execute(
        sa.text(
            "INSERT INTO sla_policy_version (activated_at, note) "
            "VALUES (now(), 'Initial policy, seeded from the v1 domain constants') "
            "RETURNING id"
        )
    ).scalar_one()

    for priority, seconds in _SEED.items():
        bind.execute(
            sa.text(
                "INSERT INTO sla_policy_entry (version_id, priority, seconds) "
                "VALUES (:vid, :priority, :seconds)"
            ),
            {"vid": version_id, "priority": priority, "seconds": seconds},
        )

    bind.execute(
        sa.text(
            "UPDATE ticket SET sla_policy_version_id = :vid WHERE sla_policy_version_id IS NULL"
        ),
        {"vid": version_id},
    )
    op.alter_column("ticket", "sla_policy_version_id", nullable=False)


def downgrade() -> None:
    """Reversible: the pin is derived data, and the durations return to code.

    Unlike 0013/0014 this does not refuse — dropping the pin loses only the
    *explanation* of a ticket's window, never the window itself, which lives in
    the frozen `deadline`.
    """
    op.drop_column("ticket", "sla_policy_version_id")
    op.execute("DROP INDEX IF EXISTS ix_sla_policy_active")
    op.drop_table("sla_policy_entry")
    op.drop_table("sla_policy_version")
