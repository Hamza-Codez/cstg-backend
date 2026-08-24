"""Saved views (spec04 §6).

A private, named filter combination. Sharing is out of scope, so every query is
scoped to the owner and another owner's id is simply absent (404), never 403 —
the same existence-hiding rule as INV-9.
"""

import uuid
from collections.abc import Sequence

from app.core.authorization import Principal
from app.core.unit_of_work import SqlAlchemyUnitOfWork
from app.domain.errors import BusinessRuleViolation, NotFound
from app.models.enums import ActorType
from app.models.saved_view import SavedView
from app.schemas.saved_view import SavedViewCreate
from app.schemas.ticket_filters import TicketFilters, authorize_filters


class SavedViewService:
    def __init__(self, uow: SqlAlchemyUnitOfWork):
        self.uow = uow

    @staticmethod
    def _require_staff(principal: Principal) -> None:
        # Staff only. A customer has two states for their requests list, so a
        # saved view would be furniture.
        if principal.type is ActorType.CUSTOMER:
            raise NotFound("Not found")

    async def list_views(self, principal: Principal) -> Sequence[SavedView]:
        self._require_staff(principal)
        return await self.uow.saved_views.list_for_owner(principal.id)

    async def create_view(self, principal: Principal, data: SavedViewCreate) -> SavedView:
        self._require_staff(principal)

        # Validated at WRITE time against the author's current role, and again
        # at execution (see `filters_for`). Both are needed: roles change, and a
        # view saved as a dispatcher must not keep granting dispatcher-only
        # filters after its owner becomes an agent.
        authorize_filters(principal, data.filters)

        existing = await self.uow.saved_views.list_for_owner(principal.id)
        if any(view.name == data.name for view in existing):
            raise BusinessRuleViolation("You already have a view with that name.")

        view = SavedView(
            id=uuid.uuid4(),
            owner_id=principal.id,
            name=data.name,
            # exclude_none so the stored object carries only what was set,
            # rather than a wall of nulls that would fail `extra="forbid"` on a
            # later model change.
            filters=data.filters.model_dump(mode="json", exclude_none=True),
        )
        self.uow.saved_views.insert(view)
        return view

    async def delete_view(self, principal: Principal, view_id: uuid.UUID) -> None:
        self._require_staff(principal)
        deleted = await self.uow.saved_views.delete_for_owner(view_id, principal.id)
        # Another owner's view is indistinguishable from one that never existed.
        if not deleted:
            raise NotFound("Saved view not found")

    async def filters_for(self, principal: Principal, view_id: uuid.UUID) -> TicketFilters:
        """Re-validate a stored view against the owner's role *now*."""
        self._require_staff(principal)
        view = await self.uow.saved_views.get_for_owner(view_id, principal.id)
        if not view:
            raise NotFound("Saved view not found")

        filters = TicketFilters.model_validate(view.filters)
        authorize_filters(principal, filters)
        return filters
