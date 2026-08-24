from collections.abc import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import Category, CustomerTier, Priority
from app.models.priority_rule import PriorityRule


class PriorityRuleRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_priority(self, tier: CustomerTier, category: Category) -> Priority | None:
        stmt = select(PriorityRule.priority).where(
            PriorityRule.tier == tier, PriorityRule.category == category
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_all(self) -> Sequence[PriorityRule]:
        """The whole matrix, ordered so the admin grid renders predictably."""
        stmt = select(PriorityRule).order_by(PriorityRule.tier, PriorityRule.category)
        return (await self.session.execute(stmt)).scalars().all()

    async def replace_all(self, rules: Iterable[tuple[CustomerTier, Category, Priority]]) -> None:
        """Upsert every supplied pair.

        Upsert rather than delete-then-insert: the table is a lookup that ticket
        creation reads, and a window where it is empty would make a concurrent
        creation fail. The caller has already checked the matrix is total.
        """
        for tier, category, priority in rules:
            stmt = insert(PriorityRule).values(tier=tier, category=category, priority=priority)
            stmt = stmt.on_conflict_do_update(
                index_elements=["tier", "category"],
                set_={"priority": stmt.excluded.priority},
            )
            await self.session.execute(stmt)
