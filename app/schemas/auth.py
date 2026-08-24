import uuid

from pydantic import BaseModel, EmailStr

from app.models.enums import ActorType, Role


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: Role
    principal_type: ActorType
    # Same value as the token's `sub`. Returned explicitly so a client can tell
    # "assigned to me" from "assigned to someone else" without parsing the JWT.
    principal_id: uuid.UUID
