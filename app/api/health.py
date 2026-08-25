"""Liveness endpoint.

Deliberately outside ``/api/v1``: it is an infrastructure concern, not part of the
versioned business contract in docs/API.md. It reports that the process is serving —
it does not probe the database, so a failing dependency does not take the container
down while it is still able to report why.
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"]


class DbHealthResponse(BaseModel):
    status: Literal["ok"]
    database: Literal["reachable"]


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/health/db", response_model=DbHealthResponse)
async def db_health(db: AsyncSession = Depends(get_db)) -> DbHealthResponse:
    try:
        await db.execute(text("SELECT 1"))
        return DbHealthResponse(status="ok", database="reachable")
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "error", "database": "unreachable", "reason": str(e)},
        ) from e
