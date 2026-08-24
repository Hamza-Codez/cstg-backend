"""audit_safeguard

Revision ID: 0006_audit_safeguard
Revises: 0005_priority_rule
Create Date: 2026-08-21 00:00:06.000000
"""

from alembic import op

revision = "0006_audit_safeguard"
down_revision = "0005_priority_rule"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE RULE ticket_event_no_update AS ON UPDATE TO ticket_event DO INSTEAD NOTHING")
    op.execute("CREATE RULE ticket_event_no_delete AS ON DELETE TO ticket_event DO INSTEAD NOTHING")


def downgrade() -> None:
    op.execute("DROP RULE ticket_event_no_update ON ticket_event")
    op.execute("DROP RULE ticket_event_no_delete ON ticket_event")
