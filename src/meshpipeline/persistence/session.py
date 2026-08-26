# Responsibility: Own the database engine, its pool and the request-scoped session.
# Boundaries: connection lifecycle; pool sizing is a deployment decision the connection budget reasons about.

from __future__ import annotations

import asyncio
import threading
import weakref
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


# ONE ENGINE PER EVENT LOOP. asyncpg binds every pooled connection to the loop that opened it.
# A single process-global engine, cached across celery task loops (each task is its own
# asyncio.run), handed later tasks connections whose futures belonged to a closed loop - the
# loop-crossing bug behind every mid-job "connection is closed". The cache is keyed by the
# loop OBJECT in a WeakKeyDictionary: a collected loop cannot alias a fresh one the way id()
# reuse can, and a closed loop's entry is never served (the running loop is by definition not
# closed, and it is the only key ever looked up). Entries for closed-but-alive loops are swept
# on the next miss; the worker's eager dispose in _run_async's finally is the primary cleanup,
# the sweep is only the backstop for paths that died before their finally.
_engines: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
# The builder research tool runs asyncio.run in a side thread - creation and eviction must be
# thread-safe, and eviction pops before disposing so a bundle is disposed at most once.
_engines_lock = threading.Lock()


def _make_engine() -> AsyncEngine:
    # Managed Postgres (Neon) requires TLS; asyncpg takes it via connect_args, not a
    # URL param (config strips libpq's sslmode). Local POSTGRES_* connections leave
    # DB_SSL_REQUIRED False and pass no ssl arg - unchanged.
    connect_args = {"ssl": True} if provcfg.DB_SSL_REQUIRED else {}
    # Pool sizing is a ROLE decision, not a constant - see persistence/connection_budget.py.
    # The API service keeps a modest pool; the pipeline JOB overrides these to a tiny pool in
    # its manifest so a one-job process cannot pin a large slice of the provider's budget.
    return create_async_engine(
        provcfg.POSTGRES_DSN,
        echo=False,
        pool_size=provcfg.DB_POOL_SIZE,
        max_overflow=provcfg.DB_MAX_OVERFLOW,
        pool_timeout=provcfg.DB_POOL_TIMEOUT,
        pool_recycle=provcfg.DB_POOL_RECYCLE,
        pool_pre_ping=True,
        connect_args=connect_args,
    )


def _terminate_stale_pool(engine: AsyncEngine) -> None:
    """Backstop teardown for an engine whose loop closed before eager disposal ran.

    The pooled connections' futures belong to a dead loop, so nothing about them can be
    awaited; asyncpg's terminate() is loop-free and tears the server side down, and the
    sync dispose(close=False) then discards the pool without touching the connections.
    Best-effort by design: every connection this misses is already unusable client-side.
    """
    try:
        pool = engine.sync_engine.pool
        queue = getattr(getattr(pool, "_pool", None), "queue", None) or ()
        for rec in list(queue):
            try:
                raw = getattr(rec, "dbapi_connection", None)
                drv = getattr(raw, "driver_connection", raw)
                if drv is not None:
                    drv.terminate()
            except Exception:
                pass
        engine.sync_engine.dispose(close=False)
    except Exception:
        pass


def _get_bundle() -> tuple[AsyncEngine, async_sessionmaker]:
    # No running loop is a LOUD error, never a fallback engine - a sync caller holding a
    # cross-loop engine is exactly the bug this module exists to prevent.
    loop = asyncio.get_running_loop()
    stale: list[AsyncEngine] = []
    with _engines_lock:
        bundle = _engines.get(loop)
        if bundle is None:
            for dead in [l for l in list(_engines) if l.is_closed()]:
                dead_bundle = _engines.pop(dead, None)
                if dead_bundle is not None:
                    stale.append(dead_bundle[0])
            engine = _make_engine()
            factory = async_sessionmaker(
                bind=engine,
                class_=AsyncSession,
                expire_on_commit=False,
                autocommit=False,
                autoflush=False,
            )
            bundle = (engine, factory)
            _engines[loop] = bundle
    for old in stale:
        _terminate_stale_pool(old)
    return bundle


def _get_engine() -> AsyncEngine:
    return _get_bundle()[0]


def _get_session_factory() -> async_sessionmaker:
    return _get_bundle()[1]


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
    """Dispose the CURRENT loop's engine, if any. The worker calls this in its task finally
    (the eager path, while the loop is still alive, so connections close properly); the API
    calls it at shutdown. Other loops' entries are untouched."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    with _engines_lock:
        bundle = _engines.pop(loop, None)
    if bundle is not None:
        await bundle[0].dispose()


def reset_session_state() -> None:
    """Test seam: drop every cached engine regardless of loop, loop-free. Entries whose loops
    are gone are terminated like any stale pool; live-loop entries are terminated too - tests
    that call this own their loops and expect a clean slate."""
    with _engines_lock:
        bundles = [b for _, b in list(_engines.items())]
        _engines.clear()
    for engine, _factory in bundles:
        _terminate_stale_pool(engine)
