# Responsibility: Verify the schema installs where it declares, whatever the session search path says.
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

import meshpipeline.settings.providers as provcfg
from meshpipeline.persistence.migration_url import sync_migration_url
from meshpipeline.runtime import migrate

INTENDED_SCHEMA = "public"
FOREIGN_SCHEMA = "other"
OWNED_ENUMS = ("jobstatus", "failedreason", "artifacttype", "reconciliationstate")


#: Resolved ONCE, at import, from the same source the runtime wrapper migrates from. Captured
#: eagerly because each test repoints `provcfg` at its own disposable database - re-reading it
#: later would answer "which database is this test using?" instead of "which server?".
_SERVER_URL = sync_migration_url(migrate._migration_source(), ssl_required=False)


def _server_url() -> str:
    return _SERVER_URL


def _admin():
    url = make_url(_server_url()).set(database="postgres")
    eng = create_engine(url, poolclass=None, isolation_level="AUTOCOMMIT")
    return eng, eng.connect()


class Disposable:

    def __init__(self, name: str):
        self.name = name
        # `str(URL)` MASKS the password as ***, which then becomes the literal
        # credential. Render explicitly.
        self.url = make_url(_server_url()).set(database=name).render_as_string(
            hide_password=False)

    def connect(self, *, search_path: str | None = None):
        eng = create_engine(self.url, isolation_level="AUTOCOMMIT")
        conn = eng.connect()
        if search_path:
            conn.execute(text(f"SET search_path TO {search_path}"))
        return eng, conn

    def sql(self, statement: str, **params):
        eng, conn = self.connect()
        try:
            return list(conn.execute(text(statement), params or {}).all())
        finally:
            conn.close()
            eng.dispose()

    def scalar(self, statement: str, **params):
        rows = self.sql(statement, **params)
        return rows[0][0] if rows else None

    # catalog probes
    def tables(self, schema: str) -> list[str]:
        return sorted(r[0] for r in self.sql(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :s AND c.relkind = 'r'", s=schema))

    def enums(self, schema: str) -> list[str]:
        return sorted(r[0] for r in self.sql(
            "SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
            "WHERE n.nspname = :s AND t.typtype = 'e'", s=schema))

    def functions(self, schema: str) -> list[str]:
        return sorted(r[0] for r in self.sql(
            "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = :s", s=schema))

    def triggers(self, schema: str) -> list[str]:
        return sorted(r[0] for r in self.sql(
            "SELECT tg.tgname FROM pg_trigger tg JOIN pg_class c ON c.oid = tg.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :s AND NOT tg.tgisinternal", s=schema))

    def indexes(self, schema: str) -> list[str]:
        return sorted(r[0] for r in self.sql(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :s AND c.relkind = 'i'", s=schema))

    def constraints(self, schema: str) -> list[tuple]:
        return sorted((r[0], r[1]) for r in self.sql(
            "SELECT con.conname, con.contype FROM pg_constraint con "
            "JOIN pg_namespace n ON n.oid = con.connamespace WHERE n.nspname = :s", s=schema))

    def version(self, schema: str = INTENDED_SCHEMA) -> str | None:
        present = self.scalar(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relname = 'alembic_version' AND n.nspname = :s", s=schema)
        if not present:
            return None
        rows = self.sql(f'SELECT version_num FROM "{schema}".alembic_version')
        return rows[0][0] if rows else None

    def structure(self, schema: str = INTENDED_SCHEMA) -> list:
        cols = self.sql(
            "SELECT table_name, column_name, data_type, is_nullable, udt_name "
            "FROM information_schema.columns WHERE table_schema = :s "
            "ORDER BY table_name, column_name", s=schema)
        labels = self.sql(
            "SELECT t.typname, e.enumlabel FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid "
            "JOIN pg_namespace n ON n.oid = t.typnamespace WHERE n.nspname = :s "
            "ORDER BY t.typname, e.enumsortorder", s=schema)
        return (list(cols) + list(labels) + [("idx",) + (i,) for i in self.indexes(schema)]
                + [("con",) + c for c in self.constraints(schema)]
                + [("fn",) + (f,) for f in self.functions(schema)]
                + [("tg",) + (t,) for t in self.triggers(schema)])


@pytest.fixture()
def disposable(monkeypatch):
    name = f"schemaauth_{uuid.uuid4().hex[:12]}"
    try:
        eng, conn = _admin()
    except Exception as exc:  # noqa: BLE001
        pytest.fail("PostgreSQL is not reachable for the schema-authority tests - they must be "
                    f"PROVISIONED, not skipped ({type(exc).__name__}: {exc})")
    try:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        conn.close()
        eng.dispose()

    db = Disposable(name)
    # point the migration machinery at THIS database for the duration of the test
    async_url = make_url(migrate._migration_source()).set(
        database=name).render_as_string(hide_password=False)
    monkeypatch.setattr(provcfg, "POSTGRES_DSN", async_url, raising=False)
    monkeypatch.setattr(provcfg, "DATABASE_URL", async_url, raising=False)
    try:
        yield db
    finally:
        eng, conn = _admin()
        try:
            conn.execute(text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :d"),
                {"d": name})
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        finally:
            conn.close()
            eng.dispose()


def _hostile(db: Disposable) -> None:
    eng, conn = db.connect()
    try:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{FOREIGN_SCHEMA}"'))
        user = make_url(db.url).username
        conn.execute(text(f'ALTER ROLE "{user}" IN DATABASE "{db.name}" '
                          f'SET search_path = {FOREIGN_SCHEMA}, public'))
    finally:
        conn.close()
        eng.dispose()


def _upgrade(rev: str = "head") -> None:
    from alembic import command
    command.upgrade(migrate._alembic_config(), rev)


def _downgrade(rev: str = "base") -> None:
    from alembic import command
    command.downgrade(migrate._alembic_config(), rev)


def _head_revision() -> str:
    from alembic.script import ScriptDirectory
    return ScriptDirectory.from_config(migrate._alembic_config()).get_current_head()


# 1, 2: hostile installation

def test_a_hostile_search_path_still_installs_into_the_intended_schema(disposable):
    _hostile(disposable)
    _upgrade()
    assert len(disposable.tables(INTENDED_SCHEMA)) > 1
    assert set(OWNED_ENUMS) <= set(disposable.enums(INTENDED_SCHEMA))
    assert disposable.version(INTENDED_SCHEMA) == _head_revision()


def test_the_foreign_schema_receives_nothing(disposable):
    _hostile(disposable)
    _upgrade()
    assert disposable.tables(FOREIGN_SCHEMA) == []
    assert disposable.enums(FOREIGN_SCHEMA) == []
    assert disposable.functions(FOREIGN_SCHEMA) == []
    assert disposable.triggers(FOREIGN_SCHEMA) == []
    assert disposable.indexes(FOREIGN_SCHEMA) == []
    assert disposable.version(FOREIGN_SCHEMA) is None, \
        "a second installation history was started in a foreign schema"


# 3, 4, 5, 6: cycles

def test_the_same_database_round_trips_twice_under_a_hostile_path(disposable):
    _hostile(disposable)
    for cycle in (1, 2):
        _upgrade()
        assert disposable.version() == _head_revision(), f"cycle {cycle}: not at head"
        _downgrade()
        assert disposable.version() is None, f"cycle {cycle}: not at base"
        assert sorted(set(OWNED_ENUMS) & set(disposable.enums(INTENDED_SCHEMA))) == []
        assert disposable.tables(INTENDED_SCHEMA) == ["alembic_version"]
    _upgrade()
    assert disposable.version() == _head_revision()


def test_same_named_sentinels_in_the_foreign_schema_survive_both_cycles(disposable):
    _hostile(disposable)
    eng, conn = disposable.connect()
    try:
        for enum_name in OWNED_ENUMS:
            conn.execute(text(f'CREATE TYPE "{FOREIGN_SCHEMA}".{enum_name} AS ENUM (\'x\',\'y\')'))
        conn.execute(text(f'CREATE TABLE "{FOREIGN_SCHEMA}".simulation_jobs (id int primary key)'))
        conn.execute(text(f'CREATE INDEX ix_jobs_cleanup ON "{FOREIGN_SCHEMA}".simulation_jobs (id)'))
        conn.execute(text(
            f'CREATE FUNCTION "{FOREIGN_SCHEMA}"._chat_sessions_set_updated_at() '
            "RETURNS trigger AS $$ BEGIN RETURN NEW; END; $$ LANGUAGE plpgsql"))
    finally:
        conn.close()
        eng.dispose()

    for _ in (1, 2):
        _upgrade()
        _downgrade()

    assert disposable.enums(FOREIGN_SCHEMA) == sorted(OWNED_ENUMS)
    assert "simulation_jobs" in disposable.tables(FOREIGN_SCHEMA)
    assert "ix_jobs_cleanup" in disposable.indexes(FOREIGN_SCHEMA)
    assert "_chat_sessions_set_updated_at" in disposable.functions(FOREIGN_SCHEMA)


def test_downgrade_removes_the_owned_objects_from_the_intended_schema(disposable):
    _upgrade()
    assert disposable.functions(INTENDED_SCHEMA) != []
    assert disposable.triggers(INTENDED_SCHEMA) != []
    _downgrade()
    assert disposable.tables(INTENDED_SCHEMA) == ["alembic_version"]
    assert sorted(set(OWNED_ENUMS) & set(disposable.enums(INTENDED_SCHEMA))) == []
    assert disposable.functions(INTENDED_SCHEMA) == []
    assert disposable.triggers(INTENDED_SCHEMA) == []


# 7, 8, 9, 10

def test_a_cycled_schema_is_structurally_identical_to_a_fresh_one(disposable):
    _upgrade()
    direct = disposable.structure()
    _downgrade()
    _upgrade()
    assert disposable.structure() == direct


def test_an_existing_installation_stays_put_and_is_not_relocated(disposable):
    _upgrade()
    before_tables = disposable.tables(INTENDED_SCHEMA)
    before_version = disposable.version()
    _hostile(disposable)                      # the role becomes hostile AFTER installation
    _upgrade()                                # a no-op upgrade on an existing installation
    assert disposable.version(INTENDED_SCHEMA) == before_version
    assert disposable.tables(INTENDED_SCHEMA) == before_tables, "objects were duplicated"
    assert disposable.version(FOREIGN_SCHEMA) is None, "the version table was relocated"
    assert disposable.tables(FOREIGN_SCHEMA) == []


def test_a_hostile_session_search_path_does_not_change_the_result(disposable):
    eng, conn = disposable.connect()
    try:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{FOREIGN_SCHEMA}"'))
        conn.execute(text(f"SET search_path TO {FOREIGN_SCHEMA}, public"))
        _upgrade()
    finally:
        conn.close()
        eng.dispose()
    assert disposable.tables(FOREIGN_SCHEMA) == []
    assert disposable.version(INTENDED_SCHEMA) == _head_revision()


def test_the_runtime_wrapper_and_direct_alembic_produce_the_same_schema(disposable):
    _hostile(disposable)
    migrate.run_migrations()
    via_wrapper = disposable.structure()
    assert disposable.version() == _head_revision()
    assert disposable.tables(FOREIGN_SCHEMA) == []
    _downgrade()
    _upgrade()
    assert disposable.structure() == via_wrapper


def test_the_schema_authority_is_declared_once_and_validated():
    import importlib.util
    from pathlib import Path

    from alembic.config import Config

    cfg = Config(migrate._resolve_alembic_assets()[0])
    assert cfg.get_main_option("version_table_schema") == INTENDED_SCHEMA

    env_path = Path(migrate._resolve_alembic_assets()[1]) / "env.py"
    spec = importlib.util.spec_from_file_location("_alembic_env_probe", env_path)
    assert spec and spec.loader
    src = env_path.read_text()
    assert "_SAFE_SCHEMA" in src and "version_table_schema" in src
    # the guard itself, exercised rather than described
    import re
    pattern = re.search(r'_SAFE_SCHEMA = re\.compile\(r"([^"]+)"\)', src)
    assert pattern, "the schema identifier guard is gone"
    rx = re.compile(pattern.group(1))
    assert rx.match("public") and rx.match("hexera")
    for hostile in ('pub"lic', "public; DROP TABLE x", "pg-catalog", "", "1schema"):
        assert not rx.match(hostile), f"{hostile!r} would have reached DDL"


# 3: a stamp is a claim about the schema, not the schema itself


def test_a_stamped_but_incomplete_schema_is_refused_with_reset_guidance(disposable):
    # THE SHAPE THIS REPOSITORY MAKES POSSIBLE: one rooted revision, edited in place. A database
    # created from an earlier form of it is stamped at head and still missing objects, so
    # `upgrade head` has nothing left to run and start-up would continue against a partial schema.
    migrate.run_migrations()
    assert "native_submission_claims" in disposable.tables(INTENDED_SCHEMA)

    eng, conn = disposable.connect()
    try:
        conn.execute(text("DROP TABLE native_submission_claims"))
    finally:
        conn.close()
        eng.dispose()

    with pytest.raises(migrate.IncompleteSchemaError) as refusal:
        migrate.run_migrations()

    detail = str(refusal.value)
    assert "native_submission_claims" in detail
    assert "docker compose down -v" in detail, "the refusal does not say how to reset"
    assert "has not been modified" in detail
    # the refusal names no credential and no password
    assert "@" not in detail and "password" not in detail.lower()
    # and it did not quietly repair the schema on its way out
    assert "native_submission_claims" not in disposable.tables(INTENDED_SCHEMA)


def test_a_complete_stamped_schema_migrates_without_complaint(disposable):
    migrate.run_migrations()
    migrate.run_migrations()          # idempotent: the second start finds nothing to do
    assert disposable.version(INTENDED_SCHEMA) == _head_revision()
    assert "native_submission_claims" in disposable.tables(INTENDED_SCHEMA)
