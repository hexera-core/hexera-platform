# Responsibility: Provision the disposable integration stack: schema, migrations and buckets.
# Boundaries: it delegates migration to the production authority rather than creating tables itself.
from __future__ import annotations

from sqlalchemy.engine import URL, make_url

# The unmanaged-schema refusal is PRODUCTION behaviour, in `migrate`. Re-exported rather than
# reimplemented: a harness with its own copy proves only that the harness refuses.
from meshpipeline.runtime.migrate import UnmanagedSchemaError  # noqa: F401


def dsn() -> URL:
    import meshpipeline.settings.providers as provcfg

    return make_url(provcfg.POSTGRES_DSN)


def _async_url(url: URL | str) -> str:
    # The async engine needs an async driver, and callers legitimately hand these helpers the bare
    # libpq coordinates the runner supplies (postgresql://...), not asyncpg's vocabulary. The driver
    # is settled here from the SAME normaliser the application uses, rather than from a second copy
    # of the rule that could drift from it - and rather than in each caller, where forgetting it
    # surfaces as "the loaded 'psycopg2' is not async" from three frames away.
    from meshpipeline.settings.env import normalize_database_url

    dsn, _ = normalize_database_url(make_url(url).render_as_string(hide_password=False))
    return dsn


async def ensure_schema(url: URL | str) -> None:
    import asyncio

    await asyncio.to_thread(upgrade_to_head, url)


def upgrade_to_head(url: URL | str) -> str:
    from sqlalchemy import create_engine, text

    from meshpipeline.persistence.migration_url import sync_migration_url
    from meshpipeline.runtime import migrate

    target = make_url(url).render_as_string(hide_password=False)
    # `alembic/env.py` resolves the URL from the provider settings - one source of truth for
    # production - so pointing THIS call at another database means pointing those settings at it
    # for the duration, not passing a second URL that env.py would ignore.
    import meshpipeline.settings.providers as provcfg
    saved = (provcfg.DATABASE_URL, provcfg.POSTGRES_DSN)
    provcfg.DATABASE_URL = provcfg.POSTGRES_DSN = target
    try:
        # THE production entry point, guard included - not `command.upgrade` directly. If this
        # harness bypassed it, every suite would be provisioned by a path production never runs.
        # in-process: production must not restyle this process's logging
        migrate.run_migrations(dsn=target, configure_logging=False)
    finally:
        provcfg.DATABASE_URL, provcfg.POSTGRES_DSN = saved

    engine = create_engine(sync_migration_url(target, ssl_required=False))
    try:
        with engine.connect() as conn:
            return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    finally:
        engine.dispose()


async def table_names(url: URL | str) -> set[str]:
    from sqlalchemy import inspect
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(_async_url(url))
    try:
        async with engine.connect() as conn:
            return set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
    finally:
        await engine.dispose()


def ensure_bucket() -> None:
    try:
        from meshpipeline.contracts.object_storage import get_object_store

        get_object_store().exists(object_key="provisioning-probe")
    except Exception:  # noqa: BLE001 - best-effort; suites assert their own storage needs
        pass


# DROP SCHEMA public CASCADE, then rebuild. The proof that `url` is disposable is re-taken here,
# against this exact URL, immediately before the DDL - not inherited from whoever built the
# fixture. `authority` defaults to the session capability so a suite that already ran through the
# supported runner needs no ceremony, but there is no way to reach the DROP without one.
async def reset_schema(url: URL | str, *, authority=None) -> None:
    import asyncio

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from tests import disposable_database as dd

    if authority is None:
        authority = session_authority()
    # BEFORE any destructive statement. A read-only identity query is all that has run so far.
    await asyncio.to_thread(dd.require, authority, url, "reset_schema")

    engine = create_async_engine(_async_url(url))
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
    finally:
        await engine.dispose()
    await asyncio.to_thread(upgrade_to_head, url)


# TRUNCATE application tables. Clearing rows between cases is ordinary, but it is exactly what
# destroyed seven pre-existing rows when the tier was pointed at the shared database, so it goes
# through the same capability as the schema reset rather than being written out per suite.
async def truncate_tables(url: URL | str, *tables: str, authority=None) -> None:
    import asyncio

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from tests import disposable_database as dd

    if not tables:
        raise ValueError("truncate_tables needs the tables to clear; there is no 'everything' form")
    if authority is None:
        authority = session_authority()
    await asyncio.to_thread(dd.require, authority, url, "truncate_tables")

    engine = create_async_engine(_async_url(url))
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {', '.join(tables)} CASCADE"))
    finally:
        await engine.dispose()


# DROP TABLE IF EXISTS, for suites that rebuild a non-application schema of their own.
async def drop_tables(url: URL | str, *tables: str, authority=None) -> None:
    import asyncio

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from tests import disposable_database as dd

    if not tables:
        raise ValueError("drop_tables needs the tables to drop")
    if authority is None:
        authority = session_authority()
    await asyncio.to_thread(dd.require, authority, url, "drop_tables")

    engine = create_async_engine(_async_url(url))
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS {', '.join(tables)} CASCADE"))
    finally:
        await engine.dispose()


#: The capability the integration session proves once, in conftest, and every destructive helper
#: re-proves against its own connection.
_SESSION_AUTHORITY = None


def set_session_authority(authority) -> None:
    global _SESSION_AUTHORITY
    _SESSION_AUTHORITY = authority


def session_authority():
    from tests import disposable_database as dd

    if _SESSION_AUTHORITY is None:
        raise dd.NotDisposableError(
            "no disposable-database authority was established for this session. The integration "
            "conftest proves one before collection; reaching a destructive helper without it "
            "means the suite is pointed at a database nobody provisioned for this run.")
    return _SESSION_AUTHORITY
