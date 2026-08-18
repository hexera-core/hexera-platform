# Responsibility: Own the database engine, its pool and the request-scoped session.
# Boundaries: connection lifecycle; pool sizing is a deployment decision the connection budget reasons about.

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

import meshpipeline.settings.providers as provcfg


class Base(DeclarativeBase):
    pass



_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker | None = None


def _get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        # Managed Postgres (Neon) requires TLS; asyncpg takes it via connect_args, not a
        # URL param (config strips libpq's sslmode). Local POSTGRES_* connections leave
        # DB_SSL_REQUIRED False and pass no ssl arg - unchanged.
        connect_args = {"ssl": True} if provcfg.DB_SSL_REQUIRED else {}
        # Pool sizing is a ROLE decision, not a constant - see persistence/connection_budget.py.
        # The API service keeps a modest pool; the pipeline JOB overrides these to a tiny pool in
        # its manifest so a one-job process cannot pin a large slice of the provider's budget.
        _engine = create_async_engine(
            provcfg.POSTGRES_DSN,
            echo=False,
            pool_size=provcfg.DB_POOL_SIZE,
            max_overflow=provcfg.DB_MAX_OVERFLOW,
            pool_timeout=provcfg.DB_POOL_TIMEOUT,
            pool_recycle=provcfg.DB_POOL_RECYCLE,
            pool_pre_ping=True,
            connect_args=connect_args,
        )
    return _engine


def _get_session_factory() -> async_sessionmaker:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=_get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autocommit=False,
            autoflush=False,
        )
    return _session_factory



@asynccontextmanager
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with _get_session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
