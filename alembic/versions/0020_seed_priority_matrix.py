"""seed the priority matrix as reference data

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-26 00:00:20.000000

The tier x category matrix is not sample data — priority is DERIVED from it
(docs/SLA_ENGINE.md §2, INV-2), so a missing row means ticket creation fails
with "Priority rule not found for customer tier and category". Migration 0005
created the table and left it empty, which put twelve rows the application
cannot run without inside an optional `scripts/seed.py` step.

That cost a production deployment: every health check stayed green — the
process was up, the database reachable — while every ticket creation returned
500, because nobody had remembered a separate seed command.

Reference data belongs in a migration. `alembic upgrade head` already runs on
every deploy, so this cannot be forgotten, and the table can no longer exist in
a state the application cannot use.

Idempotent by primary key, so it is safe on a database that was already seeded
by hand or by the script. The script keeps its own copy for local use and for
re-syncing after a matrix change; `tests/unit/test_priority_matrix_seed.py`
asserts the two never drift.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Inlined rather than imported from `scripts.seed`. A migration is a snapshot
#: of history: if the matrix is revised later that is a NEW migration, and this
#: one must keep describing what it did when it ran. Importing app code would
#: let a future edit silently rewrite the past.
PRIORITY_MATRIX: tuple[tuple[str, str, str], ...] = (
    ("ENTERPRISE", "OUTAGE", "CRITICAL"),
    ("ENTERPRISE", "BILLING", "HIGH"),
    ("ENTERPRISE", "TECHNICAL", "HIGH"),
    ("ENTERPRISE", "GENERAL", "MEDIUM"),
    ("BUSINESS", "OUTAGE", "HIGH"),
    ("BUSINESS", "BILLING", "MEDIUM"),
    ("BUSINESS", "TECHNICAL", "MEDIUM"),
    ("BUSINESS", "GENERAL", "LOW"),
    ("FREE", "OUTAGE", "MEDIUM"),
    ("FREE", "BILLING", "LOW"),
    ("FREE", "TECHNICAL", "LOW"),
    ("FREE", "GENERAL", "LOW"),
)


def upgrade() -> None:
    # ON CONFLICT DO NOTHING, not DO UPDATE: an operator who has deliberately
    # tuned a row in a running system should not have it reverted by a redeploy.
    # Filling gaps is the job here; overwriting decisions is not.
    op.execute(
        sa.text(
            """
            INSERT INTO priority_rule (tier, category, priority)
            VALUES {values}
            ON CONFLICT (tier, category) DO NOTHING
            """.format(
                values=", ".join(
                    f"('{tier}'::customertier, '{category}'::category, '{priority}'::priority)"
                    for tier, category, priority in PRIORITY_MATRIX
                )
            )
        )
    )


def downgrade() -> None:
    # Only the rows this migration is responsible for, matched on the full
    # triple: a row an operator has since retuned has a different priority and
    # is left alone rather than deleted out from under them.
    op.execute(
        sa.text(
            """
            DELETE FROM priority_rule
            WHERE (tier::text, category::text, priority::text) IN ({values})
            """.format(
                values=", ".join(
                    f"('{tier}', '{category}', '{priority}')"
                    for tier, category, priority in PRIORITY_MATRIX
                )
            )
        )
    )
