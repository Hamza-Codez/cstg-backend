"""Filter capability gate (spec04 §4).

The rule under test: a filter a principal may not use is **403, never silently
dropped**. Dropping would return that principal's *own* tickets for a query that
asked about somebody else's — a successful-looking answer to a different
question.
"""

import uuid

import pytest

from app.core.authorization import Principal
from app.domain.errors import Forbidden
from app.models.enums import ActorType, CustomerTier, Role
from app.schemas.ticket_filters import TicketFilters, authorize_filters

OTHER = uuid.uuid4()


def principal(role: Role, actor: ActorType = ActorType.USER) -> Principal:
    return Principal(id=uuid.uuid4(), type=actor, role=role, is_active=True)


CUSTOMER = principal(Role.CUSTOMER, ActorType.CUSTOMER)
AGENT = principal(Role.AGENT)
DISPATCHER = principal(Role.DISPATCHER)
ADMIN = principal(Role.ADMIN)


@pytest.mark.parametrize("actor", [DISPATCHER, ADMIN], ids=["dispatcher", "admin"])
def test_staff_wide_filters_allowed_for_dispatcher_and_admin(actor: Principal) -> None:
    authorize_filters(
        actor,
        TicketFilters(tier=CustomerTier.ENTERPRISE, assignee_id=OTHER, customer_id=OTHER),
    )


@pytest.mark.parametrize("actor", [CUSTOMER, AGENT], ids=["customer", "agent"])
@pytest.mark.parametrize(
    "filters",
    [
        TicketFilters(tier=CustomerTier.ENTERPRISE),
        TicketFilters(assignee_id=OTHER),
        TicketFilters(customer_id=OTHER),
    ],
    ids=["tier", "assignee_id", "customer_id"],
)
def test_staff_wide_filters_refused_for_customer_and_agent(
    actor: Principal, filters: TicketFilters
) -> None:
    with pytest.raises(Forbidden):
        authorize_filters(actor, filters)


def test_refusal_names_the_offending_filter() -> None:
    with pytest.raises(Forbidden, match="customer_id"):
        authorize_filters(CUSTOMER, TicketFilters(customer_id=OTHER))


@pytest.mark.parametrize(
    "actor", [CUSTOMER, AGENT, DISPATCHER, ADMIN], ids=["customer", "agent", "dispatcher", "admin"]
)
def test_ordinary_filters_are_open_to_every_role(actor: Principal) -> None:
    """Status, priority, category and search narrow a principal's *own* scope,
    so they disclose nothing and stay unrestricted."""
    authorize_filters(actor, TicketFilters(q="timeout", status="OPEN", priority="HIGH"))


def test_customer_cannot_filter_by_assigned() -> None:
    # A customer's scope is already "mine"; `assigned` only asks about routing.
    with pytest.raises(Forbidden, match="assigned"):
        authorize_filters(CUSTOMER, TicketFilters(assigned=False))


def test_unknown_filter_is_rejected_by_the_model() -> None:
    # extra="forbid": a typo'd filter must not be silently ignored, or a client
    # believes it narrowed a list that it did not.
    with pytest.raises(ValueError):
        TicketFilters(nonsense=True)  # type: ignore[call-arg]
