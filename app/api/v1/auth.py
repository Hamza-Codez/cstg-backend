from fastapi import APIRouter, Depends

from app.api.deps import get_auth_service
from app.schemas.auth import LoginRequest, TokenResponse
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest, svc: AuthService = Depends(get_auth_service)) -> TokenResponse:
    """
    Authenticate a user or customer and return a JWT.
    """
    return await svc.login(req)
