"""Customer registration (docs/API.md §3)."""

from app.core.clock import now
from app.core.security import get_password_hash
from app.core.unit_of_work import SqlAlchemyUnitOfWork
from app.domain.errors import BusinessRuleViolation
from app.models.customer import Customer
from app.models.enums import CustomerTier
from app.schemas.customer import CustomerCreate

# New accounts always start here; only an admin moves a customer up a tier,
# because tier drives priority and therefore the SLA window (SLA_ENGINE.md §2).
DEFAULT_TIER = CustomerTier.FREE


class CustomerService:
    def __init__(self, uow: SqlAlchemyUnitOfWork):
        self.uow = uow

    async def register(self, data: CustomerCreate) -> Customer:
        existing = await self.uow.customers.get_by_email(data.email)
        if existing is not None:
            # Deliberately the same wording a caller would get for a weak password:
            # a distinct "email already registered" reply turns sign-up into an
            # account-enumeration oracle.
            raise BusinessRuleViolation("Could not create an account with those details.")

        customer = Customer(
            email=data.email,
            password_hash=get_password_hash(data.password),
            name=data.name,
            tier=DEFAULT_TIER,
            created_at=now(),
        )
        self.uow.customers.insert(customer)
        return customer
