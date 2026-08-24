from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.domain.errors import Unauthenticated
from app.models.customer import Customer
from app.models.enums import ActorType, Role
from app.models.user import AppUser
from app.schemas.auth import LoginRequest, TokenResponse


class AuthService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def login(self, req: LoginRequest) -> TokenResponse:
        """
        Authenticate user or customer, verify password and active status.
        Returns a TokenResponse.
        """
        # 1. Search AppUser
        user_stmt = select(AppUser).where(AppUser.email == req.email)
        user_result = await self.session.execute(user_stmt)
        user = user_result.scalar_one_or_none()

        if user:
            if not security.verify_password(req.password, user.password_hash):
                raise Unauthenticated("Invalid credentials.")
            if not user.is_active:
                raise Unauthenticated("Invalid credentials.")

            token = security.create_access_token(
                data={
                    "sub": str(user.id),
                    "principal_type": ActorType.USER.value,
                    "role": user.role.value,
                }
            )
            return TokenResponse(
                access_token=token,
                role=user.role,
                principal_type=ActorType.USER,
                principal_id=user.id,
            )

        # 2. Search Customer
        customer_stmt = select(Customer).where(Customer.email == req.email)
        customer_result = await self.session.execute(customer_stmt)
        customer = customer_result.scalar_one_or_none()

        if customer:
            if not security.verify_password(req.password, customer.password_hash):
                raise Unauthenticated("Invalid credentials.")

            # Customers are always active
            token = security.create_access_token(
                data={
                    "sub": str(customer.id),
                    "principal_type": ActorType.CUSTOMER.value,
                    "role": Role.CUSTOMER.value,
                }
            )
            return TokenResponse(
                access_token=token,
                role=Role.CUSTOMER,
                principal_type=ActorType.CUSTOMER,
                principal_id=customer.id,
            )

        raise Unauthenticated("Invalid credentials.")
