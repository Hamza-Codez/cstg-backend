"""attachment event enum

Adds the ATTACHMENT member to the eventtype enum, and nothing else.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-24 00:00:10.000000

Why this migration contains only an enum change (spec03 §4):

PostgreSQL 16 permits ``ALTER TYPE ... ADD VALUE`` inside a transaction, but the
new value cannot be *used* in that same transaction — and Alembic wraps every
migration in one. Nothing here uses ATTACHMENT at migration time, so a combined
migration would happen to work. It would then fail in P16, where a CHECK
constraint does reference a newly added enum value. Keeping enum additions in
their own revision makes the rule uniform rather than a trap that springs once.

Note the type is named ``eventtype`` (SQLAlchemy's default naming), not
``event_type`` as the sketch in docs/DATABASE.md §2 writes it. See 0001.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The v1 members, in their original order (0001). Used to rebuild the type on
# downgrade — PostgreSQL cannot drop a value from an enum in place.
_V1_EVENT_TYPES = ("CREATED", "STATUS_CHANGE", "ASSIGNMENT", "COMMENT", "SLA_BREACH")


def upgrade() -> None:
    op.execute("ALTER TYPE eventtype ADD VALUE IF NOT EXISTS 'ATTACHMENT'")


def downgrade() -> None:
    """Rebuild eventtype without ATTACHMENT, refusing if any row uses it.

    Refusing is the honest failure mode (IMPLEMENTATION_V2.md §3.5): an
    ATTACHMENT audit row has no representation in the v1 schema, and
    ticket_event is append-only (INV-10), so there is no correct way to discard
    it automatically. An operator who genuinely wants this must decide what
    happens to those rows first.
    """
    bind = op.get_bind()
    in_use = bind.execute(
        sa.text("SELECT count(*) FROM ticket_event WHERE type = 'ATTACHMENT'")
    ).scalar_one()
    if in_use:
        raise RuntimeError(
            f"Cannot downgrade: {in_use} ticket_event row(s) use type='ATTACHMENT'. "
            "These are append-only audit records with no v1 representation; "
            "resolve them deliberately before downgrading."
        )

    op.execute("ALTER TYPE eventtype RENAME TO eventtype_old")
    sa.Enum(*_V1_EVENT_TYPES, name="eventtype").create(bind)
    op.execute(
        "ALTER TABLE ticket_event ALTER COLUMN type TYPE eventtype USING type::text::eventtype"
    )
    op.execute("DROP TYPE eventtype_old")
