"""ticket full-text search

Adds a generated tsvector column over (subject, body) and its GIN index.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-24 00:00:11.000000

Design notes (spec04 §2):

A **generated column**, not a trigger. It cannot drift: there is no code path
that updates `subject` without updating the vector, because PostgreSQL computes
it. A trigger would have to be maintained alongside every write path.

`subject` is weighted A and `body` B, so a title match outranks a passing
mention in a long description.

PERFORMANCE: adding a STORED generated column **rewrites the table**. At current
volumes this is seconds. At 10x the data it is not free, and this note is here
so that is a known cost rather than a discovery during a deploy.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE ticket ADD COLUMN search_vector tsvector
          GENERATED ALWAYS AS (
            setweight(to_tsvector('english', coalesce(subject, '')), 'A') ||
            setweight(to_tsvector('english', coalesce(body, '')),    'B')
          ) STORED
        """
    )
    op.execute("CREATE INDEX ix_ticket_search ON ticket USING GIN (search_vector)")


def downgrade() -> None:
    # Dropping the column takes the index with it, but being explicit keeps the
    # reversal readable and independent of that behaviour.
    op.execute("DROP INDEX IF EXISTS ix_ticket_search")
    op.execute("ALTER TABLE ticket DROP COLUMN IF EXISTS search_vector")
