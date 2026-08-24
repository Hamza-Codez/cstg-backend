"""enum types

Revision ID: 0001_enum_types
Revises:
Create Date: 2026-08-21 00:00:01.000000
"""

import sqlalchemy as sa

from alembic import op

revision = "0001_enum_types"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    sa.Enum("OPEN", "IN_PROGRESS", "RESOLVED", "CLOSED", name="ticketstatus").create(op.get_bind())
    sa.Enum("CRITICAL", "HIGH", "MEDIUM", "LOW", name="priority").create(op.get_bind())
    sa.Enum("CUSTOMER", "AGENT", "DISPATCHER", "ADMIN", name="role").create(op.get_bind())
    sa.Enum("ENTERPRISE", "BUSINESS", "FREE", name="customertier").create(op.get_bind())
    sa.Enum("OUTAGE", "BILLING", "TECHNICAL", "GENERAL", name="category").create(op.get_bind())
    sa.Enum("INTERNAL_NOTE", "PUBLIC_REPLY", name="commenttype").create(op.get_bind())
    sa.Enum(
        "CREATED", "STATUS_CHANGE", "ASSIGNMENT", "COMMENT", "SLA_BREACH", name="eventtype"
    ).create(op.get_bind())
    sa.Enum("CUSTOMER", "USER", "SYSTEM", name="actortype").create(op.get_bind())


def downgrade() -> None:
    sa.Enum(name="actortype").drop(op.get_bind())
    sa.Enum(name="eventtype").drop(op.get_bind())
    sa.Enum(name="commenttype").drop(op.get_bind())
    sa.Enum(name="category").drop(op.get_bind())
    sa.Enum(name="customertier").drop(op.get_bind())
    sa.Enum(name="role").drop(op.get_bind())
    sa.Enum(name="priority").drop(op.get_bind())
    sa.Enum(name="ticketstatus").drop(op.get_bind())
