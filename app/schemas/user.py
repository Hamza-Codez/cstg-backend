"""Staff (app_user) contract (docs/API.md §10)."""

import uuid
from typing import Literal

from pydantic import BaseModel, EmailStr, Field

from app.models.enums import Role


class UserSummary(BaseModel):
    """Staff as seen by a dispatcher choosing an assignee.

    Carries no password material and no timestamps — just enough to identify a
    person in a picker.
    """

    id: uuid.UUID
    name: str
    email: EmailStr
    role: Role
    is_active: bool


class UserCreate(BaseModel):
    """Create a staff member (docs/API.md §10).

    `role` excludes CUSTOMER: customers live in a separate table and `app_user`
    carries `CHECK (role <> 'CUSTOMER')` (DATABASE.md §3). Rejecting it here turns
    a database integrity error into a clear 422.
    """

    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    name: str = Field(..., min_length=1, max_length=200)
    role: Literal[Role.AGENT, Role.DISPATCHER, Role.ADMIN]


class UserUpdate(BaseModel):
    """Activate or deactivate a staff member.

    Deactivating never removes them: existing tickets keep naming their owner, and
    the audit trail must stay readable. It only stops new assignments (INV-8).
    """

    is_active: bool
