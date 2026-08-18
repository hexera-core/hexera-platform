# Responsibility: Verify an unmanaged schema or an unshipped stamp is refused without any DDL or leaked detail.
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

import meshpipeline.settings.providers as provcfg
from meshpipeline.persistence.migration_url import sync_migration_url
from meshpipeline.runtime import migrate

_SERVER = sync_migration_url(migrate._migration_source(), ssl_required=False)
SCHEMA = "public"


def _admin():
    eng = create_engine(make_url(_SERVER).set(database="postgres"), isolation_level="AUTOCOMMIT")
    return eng, eng.connect()


def _url(name: str) -> str:
    return make_url(_SERVER).set(database=name).render_as_string(hide_password=False)


def _async_url(name: str) -> str:
    return make_url(provcfg.POSTGRES_DSN).set(database=name).render_as_string(hide_password=False)


@pytest.fixture()
def db():
    name = f"guard_{uuid.uuid4().hex[:12]}"
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


def _exec(name: str, *statements: str) -> None:
    eng = create_engine(_url(name), isolation_level="AUTOCOMMIT")
    try:
        with eng.connect() as c:
            for stmt in statements:
                c.execute(text(stmt))
    finally:
        eng.dispose()


def _catalog(name: str) -> list[tuple]:
    eng = create_engine(_url(name))
    try:
        with eng.connect() as c:
            rows = c.execute(text(
                "SELECT 'rel', n.nspname, c.relname, c.relkind::text FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname NOT IN ('pg_catalog','information_schema','pg_toast') "
                "UNION ALL "
                "SELECT 'typ', n.nspname, t.typname, t.typtype::text FROM pg_type t "
                "JOIN pg_namespace n ON n.oid = t.typnamespace "
                "WHERE n.nspname NOT IN ('pg_catalog','information_schema','pg_toast') "
                "UNION ALL "
                "SELECT 'fn', n.nspname, p.proname, '' FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname NOT IN ('pg_catalog','information_schema')")).all()
            return sorted(tuple(r) for r in rows)
    finally:
        eng.dispose()


def _has_version_table(name: str) -> bool:
    eng = create_engine(_url(name))
    try:
        with eng.connect() as c:
            return bool(c.execute(text(
                "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relname = 'alembic_version' AND n.nspname = :s"), {"s": SCHEMA}).scalar())
    finally:
        eng.dispose()


def _migrate(name: str):
    import unittest.mock as _m

    target = _async_url(name)
    with _m.patch.object(provcfg, "POSTGRES_DSN", target), \
         _m.patch.object(provcfg, "DATABASE_URL", target):
        return migrate.run_migrations(configure_logging=False)


def _head() -> str:
    from alembic.script import ScriptDirectory
    return ScriptDirectory.from_config(migrate._alembic_config()).get_current_head()


def _version(name: str) -> str | None:
    if not _has_version_table(name):
        return None
    eng = create_engine(_url(name))
    try:
        with eng.connect() as c:
            return c.execute(text("SELECT version_num FROM alembic_version")).scalar()
    finally:
        eng.dispose()


#: the minimum that makes a schema recognisably Hexera's, written as explicit test DDL.
#: Not production migration DDL, and not a whole parallel schema - one owned table and one owned
#: enum type is exactly what the guard looks for.
UNMANAGED_DDL = (
    "CREATE TYPE jobstatus AS ENUM ('pending', 'running')",
    "CREATE TABLE simulation_jobs (id uuid PRIMARY KEY, status jobstatus NOT NULL)",
)


# 1, 2, 6, 7

def test_an_empty_schema_migrates_to_head(db):
    _migrate(db)
    assert _version(db) == _head()


def test_a_managed_schema_migrates_again_idempotently(db):
    _migrate(db)
    catalog = _catalog(db)
    _migrate(db)
    assert _version(db) == _head()
    assert _catalog(db) == catalog, "a second run of a managed database changed it"


def test_a_schema_holding_only_unrelated_objects_still_migrates(db):
    _exec(db, "CREATE TABLE totally_unrelated (id int PRIMARY KEY)",
              "CREATE TYPE somebody_elses_enum AS ENUM ('a', 'b')")
    _migrate(db)
    assert _version(db) == _head()


def test_same_named_objects_in_another_schema_do_not_trigger_refusal(db):
    _exec(db, 'CREATE SCHEMA "other"',
              "CREATE TYPE other.jobstatus AS ENUM ('x','y')",
              "CREATE TABLE other.simulation_jobs (id int PRIMARY KEY)")
    _migrate(db)
    assert _version(db) == _head()


# 3, 4, 5, 9

def test_an_unmanaged_hexera_schema_is_refused(db):
    _exec(db, *UNMANAGED_DDL)
    with pytest.raises(migrate.UnmanagedSchemaError) as exc:
        _migrate(db)
    message = str(exc.value)
    assert "no alembic_version" in message
    assert "simulation_jobs" in message and "jobstatus" in message
    # The refusal names the ONE remedy this pre-release build has. It must not offer to adopt the
    # schema: with a single baseline and no upgrade path, adoption is advice nobody can follow.
    assert "recreate the database empty" in message
    assert "no adoption path" in message
    assert "Do not stamp the baseline onto this schema" in message


def test_the_refused_database_is_catalog_identical(db):
    _exec(db, *UNMANAGED_DDL)
    before = _catalog(db)
    with pytest.raises(migrate.UnmanagedSchemaError):
        _migrate(db)
    assert _catalog(db) == before, "the refusal modified the database"


def test_the_refusal_introduces_no_version_table_or_row(db):
    _exec(db, *UNMANAGED_DDL)
    with pytest.raises(migrate.UnmanagedSchemaError):
        _migrate(db)
    assert not _has_version_table(db), "an unversioned schema was stamped"
    assert _version(db) is None


def test_the_refusal_exposes_no_connection_details(db):
    _exec(db, *UNMANAGED_DDL)
    with pytest.raises(migrate.UnmanagedSchemaError) as exc:
        _migrate(db)
    message = str(exc.value)
    url = make_url(_SERVER)
    assert url.password not in message or f":{url.password}@" not in message
    assert f":{url.password}@" not in message, "credentials were rendered into the message"
    assert "sslmode" not in message, "query parameters leaked"
    assert "postgresql" not in message, "a full DSN leaked"


# 8: hostile search path

def test_a_hostile_search_path_does_not_redirect_detection(db):
    user = make_url(_SERVER).username
    _exec(db, 'CREATE SCHEMA "other"',
              "CREATE TYPE other.jobstatus AS ENUM ('x','y')",
              "CREATE TABLE other.simulation_jobs (id int PRIMARY KEY)",
              f'ALTER ROLE "{user}" IN DATABASE "{db}" SET search_path = other, public')
    _migrate(db)                                  # public is empty -> must install cleanly
    assert _version(db) == _head()

    eng = create_engine(_url(db))
    try:
        with eng.connect() as c:
            in_other = c.execute(text(
                "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'other' AND c.relkind = 'r'")).scalar()
    finally:
        eng.dispose()
    assert in_other == 1, "migration wrote into the foreign schema"


# 10, 11

def test_the_provisioning_harness_delegates_to_production(db):
    import inspect as _inspect

    from tests import harness_provisioning as hp

    assert hp.UnmanagedSchemaError is migrate.UnmanagedSchemaError, \
        "the harness defines its own error type instead of production's"
    source = _inspect.getsource(hp.upgrade_to_head)
    assert "migrate.run_migrations" in source, "the harness bypasses the production entry point"

    _exec(db, *UNMANAGED_DDL)
    with pytest.raises(migrate.UnmanagedSchemaError):
        hp.upgrade_to_head(_url(db))              # refused through the harness, by production


def test_removing_the_guard_would_break_these_tests(db, monkeypatch):
    monkeypatch.setattr(migrate, "assert_schema_is_managed", lambda *a, **k: None)
    _exec(db, *UNMANAGED_DDL)
    with pytest.raises(Exception) as exc:
        _migrate(db)
    # without the guard the failure is Alembic's mid-migration collision, not a clean refusal
    assert not isinstance(exc.value, migrate.UnmanagedSchemaError)
    assert "already exists" in str(exc.value)


# superseded revision stamps
# The migration history is one initial-schema baseline. A database stamped with a revision from
# the superseded development chain describes a schema no shipped script can reason about, and
# there is deliberately no bridge back to it: Hexera has not launched, so every such database
# is disposable and the honest instruction is to recreate it.
# These do not name every removed revision - that would be a scar recording history rather than a
# contract. They assert the general rule: a stamp this build does not ship is refused, before any
# DDL, without rewriting the stamp.

#: One revision from the deleted development chain, and one that never existed. The guard must not
#: distinguish them - "we used to ship this" is not a reason to treat a stamp differently, and the
#: history cut left no upgrade path from either.
UNKNOWN_STAMPS = ("0003_source_object_cleanup", "9999_never_existed")


def _stamp(name: str, revision: str) -> None:
    _exec(name,
          "CREATE TABLE alembic_version (version_num varchar(32) NOT NULL, "
          "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))",
          f"INSERT INTO alembic_version VALUES ('{revision}')")


@pytest.mark.parametrize("revision", UNKNOWN_STAMPS)
def test_a_database_stamped_with_an_unshipped_revision_is_refused(db, revision):
    _stamp(db, revision)
    with pytest.raises(migrate.UnknownRevisionError) as exc:
        _migrate(db)
    assert revision in str(exc.value), "the refusal does not name the revision it refused"


@pytest.mark.parametrize("revision", UNKNOWN_STAMPS)
def test_the_unshipped_stamp_refusal_performs_no_ddl(db, revision):
    _stamp(db, revision)
    before = _catalog(db)
    with pytest.raises(migrate.UnknownRevisionError):
        _migrate(db)
    assert _catalog(db) == before, "the refusal modified the database"


@pytest.mark.parametrize("revision", UNKNOWN_STAMPS)
def test_the_unshipped_stamp_is_not_rewritten(db, revision):
    _stamp(db, revision)
    with pytest.raises(migrate.UnknownRevisionError):
        _migrate(db)
    assert _version(db) == revision


def test_the_unshipped_stamp_refusal_is_actionable(db):
    _stamp(db, UNKNOWN_STAMPS[0])
    with pytest.raises(migrate.UnknownRevisionError) as exc:
        _migrate(db)
    message = str(exc.value)
    assert "Recreate it against an empty database" in message
    assert "Do not restamp" in message
    assert _head() in message, "the refusal does not say which revision this build does ship"


def test_the_unshipped_stamp_refusal_exposes_no_connection_details(db):
    _stamp(db, UNKNOWN_STAMPS[0])
    with pytest.raises(migrate.UnknownRevisionError) as exc:
        _migrate(db)
    message = str(exc.value)
    url = make_url(_SERVER)
    assert f":{url.password}@" not in message, "credentials were rendered into the message"
    assert f"{url.username}:" not in message, "the username was rendered with a credential"
    assert "sslmode" not in message, "query parameters leaked"
    assert "postgresql" not in message, "a full DSN leaked"
    assert db in message and (url.host or "localhost") in message, (
        "the refusal must still say WHICH database, or the operator cannot act on it")


def test_a_database_at_the_shipped_head_is_not_refused(db):
    _migrate(db)
    assert _version(db) == _head()
    _migrate(db)
    assert _version(db) == _head()


def test_removing_the_stamp_guard_would_break_these_tests(db, monkeypatch):
    _stamp(db, UNKNOWN_STAMPS[0])
    monkeypatch.setattr(migrate, "assert_stamp_is_known", lambda *a, **k: None)
    with pytest.raises(Exception) as exc:      # noqa: PT011 - Alembic's own, whatever it is
        _migrate(db)
    assert not isinstance(exc.value, migrate.UnknownRevisionError)
    assert _version(db) == UNKNOWN_STAMPS[0], "the bypassed run rewrote the stamp"
