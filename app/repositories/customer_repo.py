import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer import Customer


class CustomerRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, customer_id: uuid.UUID) -> Customer | None:
        stmt = select(Customer).where(Customer.id == customer_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_email(self, email: str) -> Customer | None:
        """Look up by login identity. `email` is citext, so this is case-insensitive."""
        stmt = select(Customer).where(Customer.email == email)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    def insert(self, customer: Customer) -> None:
        """Stage a new customer; the caller's unit of work commits it."""
        self.session.add(customer)
