# Responsibility: Verify a cold database is unusable until migrated, and provisioning uses the production authority.
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from tests import harness_provisioning as hp

import meshpipeline.settings.providers as provcfg
from meshpipeline.persistence.migration_url import sync_migration_url
from meshpipeline.runtime import migrate

_SERVER = sync_migration_url(migrate._migration_source(), ssl_required=False)


def _admin():
    eng = create_engine(make_url(_SERVER).set(database="postgres"), isolation_level="AUTOCOMMIT")
    return eng, eng.connect()


def _url_for(name: str) -> str:
    return make_url(_SERVER).set(database=name).render_as_string(hide_password=False)


@pytest.fixture()
def cold_db():
    name = f"provcontract_{uuid.uuid4().hex[:12]}"
    eng, conn = _admin()
    try:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        conn.close(); eng.dispose()
    try:
        yield name
    finally:
        eng, conn = _admin()
        try:
            conn.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                              "WHERE datname = :d"), {"d": name})
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        finally:
            conn.close(); eng.dispose()


def _tables(name: str) -> set[str]:
    eng = create_engine(_url_for(name))
    try:
        with eng.connect() as c:
            return set(inspect(c).get_table_names())
    finally:
        eng.dispose()


def _version(name: str) -> str | None:
    eng = create_engine(_url_for(name))
    try:
        with eng.connect() as c:
            if "alembic_version" not in inspect(c).get_table_names():
                return None
            return c.execute(text("SELECT version_num FROM alembic_version")).scalar()
    finally:
        eng.dispose()


def _head() -> str:
    from alembic.script import ScriptDirectory
    return ScriptDirectory.from_config(migrate._alembic_config()).get_current_head()


# 1, 2, 3: cold -> usable

def test_a_cold_database_is_unusable_before_migration(cold_db):
    assert _tables(cold_db) == set()
    eng = create_engine(_url_for(cold_db))
    try:
        with eng.connect() as c, pytest.raises(Exception) as exc:
            c.execute(text("SELECT count(*) FROM simulation_jobs"))
        assert "simulation_jobs" in str(exc.value)
    finally:
        eng.dispose()


def test_the_same_database_becomes_usable_after_upgrade_to_head(cold_db):
    revision = hp.upgrade_to_head(_url_for(cold_db))
    assert revision == _head()
    eng = create_engine(_url_for(cold_db))
    try:
        with eng.connect() as c:
            assert c.execute(text("SELECT count(*) FROM simulation_jobs")).scalar() == 0
    finally:
        eng.dispose()


def test_an_unmigrated_database_fails_with_a_clear_provisioning_error(cold_db):
    eng = create_engine(_url_for(cold_db))
    try:
        with eng.connect() as c, pytest.raises(Exception) as exc:
            c.execute(text("SELECT * FROM geometry_sources"))
    finally:
        eng.dispose()
    assert "geometry_sources" in str(exc.value)
    assert _version(cold_db) is None, "an unmigrated database must not claim a revision"


# 4, 5, 6: unmanaged schema

#: The minimum that makes a schema recognisably Hexera's, as explicit test DDL. One owned
#: table and one owned type is exactly what the guard looks for, so nothing more is built: this
#: test is about an UNMANAGED schema, not about SQLAlchemy's table builder.
_UNMANAGED_DDL = (
    "CREATE TYPE jobstatus AS ENUM ('pending', 'running')",
    "CREATE TABLE simulation_jobs (id uuid PRIMARY KEY, status jobstatus NOT NULL)",
)


def _unmanaged_schema(name: str) -> None:
    eng = create_engine(_url_for(name), isolation_level="AUTOCOMMIT")
    try:
        with eng.connect() as conn:
            for statement in _UNMANAGED_DDL:
                conn.execute(text(statement))
    finally:
        eng.dispose()


def test_an_unmanaged_schema_is_refused(cold_db):
    _unmanaged_schema(cold_db)
    assert "simulation_jobs" in _tables(cold_db)
    assert _version(cold_db) is None
    with pytest.raises(hp.UnmanagedSchemaError) as exc:
        hp.upgrade_to_head(_url_for(cold_db))
    message = str(exc.value)
    assert "no alembic_version" in message, "the message does not say what is missing"
    assert "simulation_jobs" in message, "the message does not name what it found"
    # One baseline, no adoption path: the only remedy is an empty database, and the refusal has to
    # say so rather than suggest verifying this schema against a revision that no longer exists.
    assert "recreate the database empty" in message, "the operator is not told what to do"
    assert "no adoption path" in message
    assert "Do not stamp the baseline onto this schema" in message


def test_the_refusal_does_not_mutate_or_adopt_the_database(cold_db):
    _unmanaged_schema(cold_db)
    before = _tables(cold_db)
    with pytest.raises(hp.UnmanagedSchemaError):
        hp.upgrade_to_head(_url_for(cold_db))
    assert _tables(cold_db) == before, "the database was modified while being refused"
    assert _version(cold_db) is None, "an unversioned schema was silently stamped"


def test_the_refusal_leaks_no_credentials(cold_db):
    _unmanaged_schema(cold_db)
    with pytest.raises(hp.UnmanagedSchemaError) as exc:
        hp.upgrade_to_head(_url_for(cold_db))
    url = make_url(_SERVER)
    message = str(exc.value)
    # Production emits no DSN at all - just host:port/database, which is what an operator needs
    # in order to act. Nothing that could authenticate, and no query parameters (which carry
    # sslmode and, on hosted providers, endpoint keys).
    assert f":{url.password}@" not in message, "credentials were rendered into the message"
    assert "postgresql" not in message, "a full DSN leaked"
    assert "sslmode" not in message, "query parameters leaked"
    assert str(url.host) in message and str(url.database or "") not in ("", None)


# warm behaviours

def test_a_genuinely_empty_database_migrates(cold_db):
    assert hp.upgrade_to_head(_url_for(cold_db)) == _head()


def test_an_alembic_managed_warm_database_is_idempotent(cold_db):
    first = hp.upgrade_to_head(_url_for(cold_db))
    tables = _tables(cold_db)
    second = hp.upgrade_to_head(_url_for(cold_db))       # warm, already at head
    assert first == second == _head()
    assert _tables(cold_db) == tables, "a warm re-run changed the schema"


# 7, 8: isolation

def test_a_destructive_reset_cannot_touch_another_database():
    import asyncio
    import os

    from tests import disposable_database as dd

    # The reset target is provisioned through the disposable authority, so it carries the marker
    # the destructive helpers require. The sibling is deliberately NOT provisioned that way.
    run_id = dd.new_run_id()
    os.environ[dd.RUN_ID_ENV] = run_id
    admin = make_url(provcfg.POSTGRES_DSN).set(database="postgres")
    target = dd.provision(admin, run_id)

    other = f"provcontract_{uuid.uuid4().hex[:12]}"
    eng, conn = _admin()
    try:
        conn.execute(text(f'CREATE DATABASE "{other}"'))
    finally:
        conn.close(); eng.dispose()
    try:
        hp.upgrade_to_head(_url_for(other))
        witness = _tables(other)
        assert "simulation_jobs" in witness
        eng, conn = _admin(); conn.close(); eng.dispose()
        # a sentinel the refusal must leave untouched
        e2 = create_engine(dd._sync_url(_url_for(other)), isolation_level="AUTOCOMMIT")
        with e2.connect() as c:
            c.execute(text("CREATE TABLE prov_sentinel (id int)"))
            c.execute(text("INSERT INTO prov_sentinel VALUES (1)"))
        e2.dispose()

        # 1) the marked database resets, through the real authority
        authority = dd.authorize(target.url, run_id)
        async_target = make_url(target.url).set(
            drivername="postgresql+asyncpg").render_as_string(hide_password=False)
        asyncio.run(hp.reset_schema(async_target, authority=authority))
        assert "simulation_jobs" in _tables(target.database)

        # 2) the unmarked sibling is refused BEFORE any destructive statement
        async_other = make_url(_url_for(other)).set(
            drivername="postgresql+asyncpg").render_as_string(hide_password=False)
        with pytest.raises(dd.NotDisposableError):
            asyncio.run(hp.reset_schema(async_other, authority=None))
        assert _tables(other) == witness | {"prov_sentinel"}, \
            "resetting one database disturbed another"
        assert _version(other) == _head()
        e2 = create_engine(dd._sync_url(_url_for(other)), isolation_level="AUTOCOMMIT")
        with e2.connect() as c:
            assert c.execute(text("SELECT count(*) FROM prov_sentinel")).scalar_one() == 1
        e2.dispose()

        # 3) a different run identity cannot reuse this authority
        with pytest.raises(dd.NotDisposableError, match="is marked for run"):
            dd.authorize(target.url, dd.new_run_id())
    finally:
        os.environ.pop(dd.RUN_ID_ENV, None)
        eng, conn = _admin()
        try:
            for name in (other, target.database):
                conn.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                                  "WHERE datname = :d"), {"d": name})
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        finally:
            conn.close(); eng.dispose()


# 9: event loops

def test_two_sequential_event_loops_do_not_share_an_engine(cold_db):
    import asyncio

    from meshpipeline.persistence.session import dispose_engine, get_db

    hp.upgrade_to_head(_url_for(cold_db))
    async_url = make_url(provcfg.POSTGRES_DSN).set(
        database=cold_db).render_as_string(hide_password=False)

    async def touch():
        async with get_db() as db:
            await db.execute(text("SELECT 1"))
        await dispose_engine()

    import unittest.mock as _m
    with _m.patch.object(provcfg, "POSTGRES_DSN", async_url), \
         _m.patch.object(provcfg, "DATABASE_URL", async_url):
        asyncio.run(touch())      # loop A
        asyncio.run(touch())      # loop B - must not inherit A's pooled connections


# 10..15

def test_fresh_and_existing_installs_use_the_same_authority(cold_db):
    import unittest.mock as _m
    async_url = make_url(provcfg.POSTGRES_DSN).set(
        database=cold_db).render_as_string(hide_password=False)
    with _m.patch.object(provcfg, "POSTGRES_DSN", async_url), \
         _m.patch.object(provcfg, "DATABASE_URL", async_url):
        migrate.run_migrations()                 # fresh
        fresh = _tables(cold_db)
        migrate.run_migrations()                 # existing
        assert _tables(cold_db) == fresh
    assert _version(cold_db) == _head()


def test_orm_and_migration_schemas_agree_after_provisioning(cold_db):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from meshpipeline.persistence.models import Base

    hp.upgrade_to_head(_url_for(cold_db))
    eng = create_engine(_url_for(cold_db))
    try:
        with eng.connect() as c:
            ctx = MigrationContext.configure(c, opts={"version_table_schema": "public",
                                                      "include_schemas": False})
            # `alembic_version` is Alembic's own bookkeeping and is deliberately absent from the
            # ORM model, so it is not schema drift.
            diff = [d for d in compare_metadata(ctx, Base.metadata)
                    if "alembic_version" not in repr(d)]
    finally:
        eng.dispose()
    assert diff == [], f"migrated schema drifted from the ORM model: {diff}"


def test_a_hostile_ambient_search_path_cannot_redirect_provisioning(cold_db):
    eng = create_engine(_url_for(cold_db), isolation_level="AUTOCOMMIT")
    try:
        with eng.connect() as c:
            c.execute(text('CREATE SCHEMA "other"'))
            user = make_url(_SERVER).username
            c.execute(text(f'ALTER ROLE "{user}" IN DATABASE "{cold_db}" '
                           "SET search_path = other, public"))
    finally:
        eng.dispose()
    assert hp.upgrade_to_head(_url_for(cold_db)) == _head()
    eng = create_engine(_url_for(cold_db))
    try:
        with eng.connect() as c:
            in_other = c.execute(text(
                "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'other'")).scalar()
    finally:
        eng.dispose()
    assert in_other == 0, "provisioning was redirected into a foreign schema"
