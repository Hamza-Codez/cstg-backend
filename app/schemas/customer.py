"""Customer registration contract (docs/API.md §3)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.models.enums import CustomerTier


class CustomerCreate(BaseModel):
    """Self-service sign-up.

    `tier` is deliberately absent: a customer must not be able to award
    themselves ENTERPRISE, which would buy them a 2h SLA. New accounts start on
    FREE and are moved by an admin.
    """

    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    name: str = Field(..., min_length=1, max_length=200)


class CustomerResponse(BaseModel):
    id: uuid.UUID
    email: EmailStr
    name: str
    tier: CustomerTier
    created_at: datetime
