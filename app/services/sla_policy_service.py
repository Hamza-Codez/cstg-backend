"""Versioned SLA policy (spec06).

Durations become data without letting them rewrite history. Because `deadline`
is frozen at creation (INV-1), a policy change is *already* safe for existing
tickets; versioning adds the ability to explain the number they were given.
"""

import uuid
from collections.abc import Mapping
from datetime import timedelta

from app.core.authorization import Principal
from app.core.clock import now
from app.core.unit_of_work import SqlAlchemyUnitOfWork
from app.domain.errors import BusinessRuleViolation, NotFound, StateConflict
from app.models.enums import Priority
from app.models.sla_policy import SlaPolicyEntry, SlaPolicyVersion

#: Bounds a duration must satisfy. The upper bound exists so a typo cannot
#: quietly promise a ninety-year response time.
MIN_SECONDS = 60
MAX_SECONDS = 90 * 24 * 3600


class SlaPolicyService:
    def __init__(self, uow: SqlAlchemyUnitOfWork):
        self.uow = uow

    async def active(self) -> tuple[SlaPolicyVersion, dict[Priority, timedelta]]:
        """The live policy and its durations.

        Raises rather than falling back to the domain defaults: a system with no
        active policy is misconfigured, and inventing durations at runtime would
        hide that behind tickets carrying terms nobody chose.
        """
        version = await self.uow.sla_policies.active_version()
        if version is None:
            raise NotFound("No active SLA policy is configured.")
        return version, await self.uow.sla_policies.entries_for(version.id)

    @staticmethod
    def validate(durations: Mapping[Priority, int]) -> None:
        """A policy must be **total** across Priority (spec06 §4).

        The same rule the priority matrix follows: a partial mapping is a
        configuration error rejected at write time, never a runtime default.
        Submitting the whole policy at once is what keeps it total — a sequence
        of per-priority edits would pass through states that are not.
        """
        missing = [p.value for p in Priority if p not in durations]
        if missing:
            raise BusinessRuleViolation(
                f"Set a response time for all four priorities. Missing: {', '.join(missing)}."
            )
        for priority, seconds in durations.items():
            if not MIN_SECONDS <= seconds <= MAX_SECONDS:
                raise BusinessRuleViolation(
                    f"{priority.value} must be between 1 minute and 90 days."
                )

    async def activate(
        self, principal: Principal, durations: Mapping[Priority, int], note: str | None = None
    ) -> SlaPolicyVersion:
        """Publish a new policy version.

        Nothing recomputes any existing `deadline` or `sla_due_at`. That is the
        single thing this operation must never do, and it is asserted by test
        rather than trusted.

        Concurrency is arbitrated by `ix_sla_policy_active`, not by a lock: two
        simultaneous activations mean one transaction loses on the constraint.
        """
        self.validate(durations)

        moment = now()
        current = await self.uow.sla_policies.active_version()

        # Supersede FIRST. `ix_sla_policy_active` is checked per statement, not
        # at commit, so inserting the new active row while the old one is still
        # active violates it immediately — even inside one transaction.
        if current is not None:
            await self.uow.sla_policies.supersede(current.id, moment)

        version = SlaPolicyVersion(
            id=uuid.uuid4(),
            created_by=principal.id,
            activated_at=moment,
            note=note,
        )
        self.uow.sla_policies.insert_version(version)

        try:
            # Flush before the entries: they carry a FK to this row, and an
            # autoflush would otherwise try to write children before the parent.
            await self.uow.flush()

            for priority, seconds in durations.items():
                self.uow.sla_policies.insert_entry(
                    SlaPolicyEntry(version_id=version.id, priority=priority, seconds=seconds)
                )
            await self.uow.flush()
        except Exception as exc:  # pragma: no cover - exercised by the race test
            if "ix_sla_policy_active" in str(exc):
                raise StateConflict(
                    "Another admin just changed this. Refresh to see the latest."
                ) from exc
            raise

        return version
