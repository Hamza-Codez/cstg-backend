"""Aggregates every v1 router under ``/api/v1`` (docs/API.md §1)."""

from fastapi import APIRouter

from app.api.v1.auth import router as auth_router
from app.api.v1.configuration import router as configuration_router
from app.api.v1.customers import router as customers_router
from app.api.v1.metrics import router as metrics_router
from app.api.v1.tickets import router as tickets_router
from app.api.v1.users import router as users_router

router = APIRouter(prefix="/api/v1")
router.include_router(auth_router)
router.include_router(customers_router)
router.include_router(tickets_router)
router.include_router(users_router)
router.include_router(metrics_router)
router.include_router(configuration_router)
