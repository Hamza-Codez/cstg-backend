from app.domain.priority import resolve
from app.models.enums import Category, CustomerTier, Priority

# We can duplicate the matrix for testing or mock it
from scripts.seed import PRIORITY_MATRIX


def test_priority_matrix_overrides() -> None:
    assert resolve(CustomerTier.ENTERPRISE, Category.OUTAGE, PRIORITY_MATRIX) == Priority.CRITICAL
    assert resolve(CustomerTier.FREE, Category.GENERAL, PRIORITY_MATRIX) == Priority.LOW


def test_priority_matrix_default() -> None:
    """Verify that every combination of Tier x Category maps to a Priority."""
    for tier in CustomerTier:
        for cat in Category:
            # this will raise KeyError if missing
            priority = resolve(tier, cat, PRIORITY_MATRIX)
            assert isinstance(priority, Priority)
