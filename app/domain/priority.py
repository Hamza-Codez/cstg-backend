from collections.abc import Mapping

from app.models.enums import Category, CustomerTier, Priority


def resolve(
    tier: CustomerTier, category: Category, rules: Mapping[tuple[CustomerTier, Category], Priority]
) -> Priority:
    """
    Resolve priority deterministically from tier and category.
    The mapping must be total, so KeyError implies a missing seed configuration.
    """
    return rules[(tier, category)]
