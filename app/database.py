"""Async engine, session factory, and the declarative Base.

Holds no business logic and knows nothing about tickets — models arrive in P1.
"""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for every ORM model (docs/BACKEND_STRUCTURE.md §2)."""


def create_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    return create_async_engine(database_url, echo=echo, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False keeps ORM objects usable after a service commits its
    # transaction, so routers can serialize the result without a second round trip.
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
