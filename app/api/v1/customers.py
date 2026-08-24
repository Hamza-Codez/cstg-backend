"""Customer registration (docs/API.md §3).

Unauthenticated by design: this is how a customer account comes into existence.
Staff accounts are never created here — `app_user` carries
`CHECK (role <> 'CUSTOMER')` and is managed by an admin (P11).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.unit_of_work import SqlAlchemyUnitOfWork
from app.schemas.customer import CustomerCreate, CustomerResponse
from app.services.customer_service import CustomerService

router = APIRouter(prefix="/customers", tags=["Customers"])


async def get_customer_service(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> CustomerService:
    return CustomerService(SqlAlchemyUnitOfWork(session))


@router.post("", response_model=CustomerResponse, status_code=status.HTTP_201_CREATED)
async def register_customer(
    data: CustomerCreate,
    service: Annotated[CustomerService, Depends(get_customer_service)],
) -> CustomerResponse:
    """Create a customer account. New accounts start on the FREE tier."""
    async with service.uow:
        customer = await service.register(data)
    return CustomerResponse.model_validate(customer, from_attributes=True)
