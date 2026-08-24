"""Priority matrix and SLA reference (docs/API.md §11). Admin-only."""

from app.core.unit_of_work import SqlAlchemyUnitOfWork
from app.domain.errors import BusinessRuleViolation
from app.models.enums import Category, CustomerTier, Priority
from app.schemas.configuration import (
    ConfigurationResponse,
    PriorityMatrixUpdate,
    PriorityRuleEntry,
    SlaDurationEntry,
    SlaPolicySummary,
)
from app.services.sla_policy_service import SlaPolicyService

# Every pair the matrix must define (SLA_ENGINE.md §2).
REQUIRED_PAIRS = {(tier, category) for tier in CustomerTier for category in Category}


class ConfigurationService:
    def __init__(self, uow: SqlAlchemyUnitOfWork):
        self.uow = uow

    async def get_configuration(self) -> ConfigurationResponse:
        rules = await self.uow.priority_rules.list_all()
        version, durations = await SlaPolicyService(self.uow).active()

        entries = [
            SlaDurationEntry(priority=p, seconds=int(durations[p].total_seconds()))
            for p in Priority
        ]
        return ConfigurationResponse(
            priority_rules=[
                PriorityRuleEntry(tier=r.tier, category=r.category, priority=r.priority)
                for r in rules
            ],
            sla_durations=entries,
            sla_policy=SlaPolicySummary(
                version_id=version.id,
                activated_at=version.activated_at,
                note=version.note,
                durations=entries,
            ),
        )

    async def replace_priority_matrix(self, data: PriorityMatrixUpdate) -> ConfigurationResponse:
        supplied = {(rule.tier, rule.category) for rule in data.rules}

        if len(supplied) != len(data.rules):
            raise BusinessRuleViolation("The matrix contains a duplicate tier and category pair")

        missing = REQUIRED_PAIRS - supplied
        if missing:
            # Refusing an incomplete matrix is the whole point: a missing pair must
            # fail here, never become a runtime guess at ticket creation.
            readable = ", ".join(sorted(f"{tier}/{category}" for tier, category in missing))
            raise BusinessRuleViolation(f"The matrix must cover every pair; missing: {readable}")

        unexpected = supplied - REQUIRED_PAIRS
        if unexpected:
            raise BusinessRuleViolation("The matrix contains an unknown tier or category")

        await self.uow.priority_rules.replace_all(
            [(r.tier, r.category, r.priority) for r in data.rules]
        )
        return await self.get_configuration()
