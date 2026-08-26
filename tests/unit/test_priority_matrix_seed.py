"""The migration-seeded matrix must match the application's own (INV-2).

Migration 0020 inlines the matrix rather than importing `scripts.seed`, because
a migration is a snapshot of history: revising the matrix later is a NEW
migration, and importing app code would let a future edit silently rewrite what
an old migration claims to have done.

The cost of that correctness is a second copy, and a second copy drifts. This
test is what stops it — if the two disagree, the fix is a new migration, not an
edit to 0020.
"""

import importlib.util
import pathlib

from scripts.seed import PRIORITY_MATRIX as APP_MATRIX


def _load_migration() -> object:
    path = (
        pathlib.Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "0020_seed_priority_matrix.py"
    )
    spec = importlib.util.spec_from_file_location("_m0020", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_matrix_matches_the_application_matrix() -> None:
    migration = _load_migration()
    from_migration = {
        (tier, category): priority
        for tier, category, priority in migration.PRIORITY_MATRIX  # type: ignore[attr-defined]
    }
    from_app = {
        (tier.value, category.value): priority.value
        for (tier, category), priority in APP_MATRIX.items()
    }

    assert from_migration == from_app, (
        "The matrix in migration 0020 has drifted from scripts.seed. "
        "Do not edit 0020 — it records what already ran. Add a new migration."
    )


def test_the_migration_matrix_is_total() -> None:
    """Every tier x category pair is present.

    A gap is not a missing feature: it is a 500 on ticket creation for that
    exact combination, while every health check stays green.
    """
    from app.models.enums import Category, CustomerTier

    migration = _load_migration()
    pairs = {
        (tier, category)
        for tier, category, _priority in migration.PRIORITY_MATRIX  # type: ignore[attr-defined]
    }

    expected = {(tier.value, category.value) for tier in CustomerTier for category in Category}
    assert pairs == expected
