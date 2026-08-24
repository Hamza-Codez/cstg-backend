"""Liveness endpoint.

Deliberately outside ``/api/v1``: it is an infrastructure concern, not part of the
versioned business contract in docs/API.md. It reports that the process is serving —
it does not probe the database, so a failing dependency does not take the container
down while it is still able to report why.
"""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"]


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")
