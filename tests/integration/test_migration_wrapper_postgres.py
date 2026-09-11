# Responsibility: Verify the production migration wrapper upgrades, reverses and re-upgrades a real database.
# Boundaries: it runs from an arbitrary directory against a hostile config, and releases its lock on failure.
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

import meshpipeline.settings.providers as provcfg
from meshpipeline.persistence.migration_url import sync_migration_url
from meshpipeline.runtime import migrate


def _sync_url() -> str:
    # the local throwaway Postgres has no TLS; force ssl_required=False for the test's own conns
    return sync_migration_url(provcfg.POSTGRES_DSN, ssl_required=False)


def _connect_autocommit():
    eng = create_engine(_sync_url(), pool_pre_ping=True)
    return eng, eng.connect().execution_options(isolation_level="AUTOCOMMIT")


def _alembic_head() -> str:
    from alembic.script import ScriptDirectory
    # cwd-independent: migrate._alembic_config() resolves alembic.ini + script_location absolutely
    return ScriptDirectory.from_config(migrate._alembic_config()).get_current_head()


def _head_in_db() -> str | None:
    eng = create_engine(_sync_url())
    try:
        with eng.connect() as c:
            return c.execute(text("SELECT version_num FROM alembic_version")).scalar()
    finally:
        eng.dispose()


def _schema_fingerprint() -> tuple:
    eng = create_engine(_sync_url())
    try:
        with eng.connect() as c:
            cols = c.execute(text(
                "SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, "
                "       pg_get_expr(d.adbin, d.adrelid), a.attidentity "
                "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
                "WHERE n.nspname = 'public' AND a.attnum > 0 AND NOT a.attisdropped "
                "  AND c.relkind = 'r' AND c.relname <> 'alembic_version' "
                "ORDER BY 1, 2")).all()
            enums = c.execute(text(
                "SELECT t.typname, e.enumsortorder, e.enumlabel FROM pg_type t "
                "JOIN pg_enum e ON e.enumtypid = t.oid "
                "JOIN pg_namespace n ON n.oid = t.typnamespace "
                "WHERE n.nspname = 'public' ORDER BY 1, 2")).all()
            idx = c.execute(text(
                "SELECT i.relname, pg_get_indexdef(x.indexrelid) FROM pg_index x "
                "JOIN pg_class i ON i.oid = x.indexrelid "
                "JOIN pg_class c ON c.oid = x.indrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname <> 'alembic_version' "
                "ORDER BY 1")).all()
            cons = c.execute(text(
                "SELECT con.conname, pg_get_constraintdef(con.oid) FROM pg_constraint con "
                "JOIN pg_class c ON c.oid = con.conrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname <> 'alembic_version' "
                "ORDER BY 1")).all()
            other = c.execute(text(
                "SELECT 'seq', c.relname, '' FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind = 'S' "
                "UNION ALL "
                "SELECT 'fn', p.proname, pg_get_functiondef(p.oid) FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' "
                "ORDER BY 1, 2")).all()
        return (tuple(map(tuple, cols)), tuple(map(tuple, enums)), tuple(map(tuple, idx)),
                tuple(map(tuple, cons)), tuple(map(tuple, other)))
    finally:
        eng.dispose()


from tests.harness_provisioning import _MIGRATION_OWNED_SCHEMAS


@pytest.fixture()
def empty_db():
    try:
        eng, conn = _connect_autocommit()
    except Exception as exc:  # noqa: BLE001
        pytest.fail("PostgreSQL is not reachable for the migration integration test - it must be "
                    f"PROVISIONED, not skipped ({type(exc).__name__}: {exc})")
    try:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
        # EVERY SCHEMA THE MIGRATIONS CREATE, not just `public` - the same reason reset_schema in
        # tests/harness_provisioning.py drops them. A schema left standing survives into the
        # upgrade this fixture exists to set up, and the first CREATE TABLE inside it fails with
        # "relation already exists". The list is imported rather than repeated so the two reset
        # paths cannot disagree about what "empty" means.
        for schema in _MIGRATION_OWNED_SCHEMAS:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
    finally:
        conn.close()
        eng.dispose()
    yield


def test_run_migrations_uses_the_async_application_dsn(empty_db):
    assert provcfg.POSTGRES_DSN.startswith("postgresql+asyncpg://"), \
        f"expected the async application DSN, got {provcfg.POSTGRES_DSN!r}"
    migrate.run_migrations()  # default dsn = provcfg.POSTGRES_DSN (async)
    assert _head_in_db() == _alembic_head()


def test_empty_database_upgrades_to_head(empty_db):
    migrate.run_migrations()
    assert _head_in_db() == _alembic_head(), "an empty database did not reach Alembic head"


def test_migration_is_reversible_head_base_head_base_head(empty_db):
    from alembic import command
    cfg = migrate._alembic_config()

    heads, bases = [], []
    command.upgrade(cfg, "head")
    assert _head_in_db() == _alembic_head()
    heads.append(_schema_fingerprint())

    for cycle in (1, 2):
        command.downgrade(cfg, "base")
        assert _head_in_db() is None, f"cycle {cycle}: downgrade did not clear the alembic version"

        eng = create_engine(_sync_url())
        try:
            with eng.connect() as c:
                # scoped to public: an identically named type in ANOTHER schema is not ours, and
                # asserting across all schemas would fail on someone else's `jobstatus`
                leftover = list(c.execute(text(
                    "SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace"
                    " WHERE n.nspname = 'public' AND t.typname IN "
                    "('jobstatus','failedreason','artifacttype','reconciliationstate')")).scalars())
                seqs = list(c.execute(text(
                    "SELECT c.relname FROM pg_class c JOIN pg_namespace n "
                    "ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relkind = 'S'")).scalars())
                fns = list(c.execute(text(
                    "SELECT p.proname FROM pg_proc p JOIN pg_namespace n "
                    "ON n.oid = p.pronamespace WHERE n.nspname = 'public'")).scalars())
        finally:
            eng.dispose()
        assert leftover == [], (
            f"cycle {cycle}: downgrade left orphan ENUM types (the re-upgrade would fail): "
            f"{leftover}")
        assert seqs == [], f"cycle {cycle}: downgrade left orphan sequences: {seqs}"
        assert fns == [], f"cycle {cycle}: downgrade left orphan functions: {fns}"
        bases.append(_schema_fingerprint())

        command.upgrade(cfg, "head")   # without the type-drop fix this raises DuplicateObject
        assert _head_in_db() == _alembic_head()
        heads.append(_schema_fingerprint())

    assert heads[0] == heads[1] == heads[2], (
        "the schema is not identical across re-creations - a cycle changed what upgrade builds")
    assert bases[0] == bases[1], "the two base states differ - residue accumulated across cycles"


def test_running_again_is_harmless(empty_db):
    migrate.run_migrations()
    migrate.run_migrations()  # already at head - must not error
    assert _head_in_db() == _alembic_head()


def _plant_hostile_alembic(dir_: os.PathLike[str]) -> None:
    d = Path(dir_)
    (d / "alembic.ini").write_text("[alembic]\nscript_location = alembic\n")
    (d / "alembic" / "versions").mkdir(parents=True, exist_ok=True)
    (d / "alembic" / "env.py").write_text("raise SystemExit('hostile env.py must never run')\n")


def test_run_migrations_from_an_arbitrary_cwd_with_a_hostile_config(empty_db, tmp_path, monkeypatch):
    _plant_hostile_alembic(tmp_path)
    monkeypatch.chdir(tmp_path)
    migrate.run_migrations()
    assert _head_in_db() == _alembic_head()


def test_module_entry_point_from_an_arbitrary_cwd_with_a_hostile_config(empty_db, tmp_path):
    _plant_hostile_alembic(tmp_path)
    p = subprocess.run([sys.executable, "-m", "meshpipeline.runtime.migrate"],
                       cwd=str(tmp_path), env={**os.environ}, capture_output=True, text=True, timeout=180)
    assert p.returncode == 0, f"migrate failed from cwd={tmp_path}:\n{p.stderr}"
    assert "hostile" not in (p.stdout + p.stderr), "the planted config was used"
    assert _head_in_db() == _alembic_head()


def test_explicit_alembic_config_override_is_used(empty_db, tmp_path, monkeypatch):
    trusted_ini, _ = migrate._resolve_alembic_assets()
    monkeypatch.setenv("ALEMBIC_CONFIG", trusted_ini)
    monkeypatch.chdir(tmp_path)
    migrate.run_migrations()
    assert _head_in_db() == _alembic_head()


def test_two_concurrent_invocations_do_not_run_migrations_at_once(empty_db):
    inside = {"n": 0, "overlap": False}
    guard = threading.Lock()

    def critical():
        with guard:
            inside["n"] += 1
            if inside["n"] > 1:
                inside["overlap"] = True
        time.sleep(0.4)
        with guard:
            inside["n"] -= 1

    def worker():
        eng, conn = _connect_autocommit()
        try:
            migrate._run_locked(conn, critical)
        finally:
            conn.close()
            eng.dispose()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert not inside["overlap"], "two holders of the real advisory lock ran concurrently"


def test_concurrent_run_migrations_both_succeed(empty_db):
    errors: list[Exception] = []

    def worker():
        try:
            migrate.run_migrations()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, f"concurrent run_migrations() raised: {errors}"
    assert _head_in_db() == _alembic_head()


def test_a_failing_migration_releases_the_lock_and_raises(empty_db):
    def boom():
        raise RuntimeError("migration failed on purpose")

    eng, conn = _connect_autocommit()
    try:
        with pytest.raises(RuntimeError, match="on purpose"):
            migrate._run_locked(conn, boom)
    finally:
        conn.close()
        eng.dispose()

    acquired: list[int] = []
    eng2, conn2 = _connect_autocommit()
    try:
        migrate._run_locked(conn2, lambda: acquired.append(1))
    finally:
        conn2.close()
        eng2.dispose()
    assert acquired == [1], "the advisory lock was not released after a failed migration"


def test_production_rejects_a_local_compose_database():
    with pytest.raises(SystemExit, match="local host"):
        migrate.run_migrations(dsn="postgresql+asyncpg://u:p@postgres:5432/mesh", env_name="production")


def test_module_entry_point_succeeds(empty_db):
    p = subprocess.run([sys.executable, "-m", "meshpipeline.runtime.migrate"],
                       env={**os.environ}, capture_output=True, text=True, timeout=180)
    assert p.returncode == 0, f"`python -m meshpipeline.runtime.migrate` failed:\n{p.stderr}"
    assert _head_in_db() == _alembic_head()


def test_module_entry_point_exits_nonzero_on_failure():
    env = {**os.environ, "DATABASE_URL": "", "POSTGRES_HOST": "nonexistent-db-host.invalid"}
    p = subprocess.run([sys.executable, "-m", "meshpipeline.runtime.migrate"],
                       env=env, capture_output=True, text=True, timeout=90)
    assert p.returncode != 0, "a failed migration must exit non-zero"


def test_migrated_schema_matches_the_orm_model(empty_db):
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    import meshpipeline.persistence.models  # noqa: F401  (registers every table on Base.metadata)
    from meshpipeline.persistence.session import Base

    migrate.run_migrations()
    eng = create_engine(_sync_url())
    try:
        with eng.connect() as conn:
            diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    finally:
        eng.dispose()
    assert diff == [], f"migrated schema drifted from the ORM model (add a migration): {diff}"


# enum ownership on downgrade
# The baseline creates four PostgreSQL ENUM types as a side effect of its table columns, and
# `drop_table` does not remove them. Dropping them is therefore the baseline's own job.
# The subtle half is WHICH `jobstatus` gets dropped. An unqualified `DROP TYPE jobstatus`
# resolves through `search_path`, so under `search_path = other, public` it deleted
# `other.jobstatus` - a type this migration never created - and left `public.jobstatus` behind,
# after which the next upgrade failed with "type jobstatus already exists". Both halves were
# measured on a real database.

#: exactly the types 0001 creates. Named, never discovered - a migration owns what it created.
OWNED_ENUMS = ("jobstatus", "failedreason", "artifacttype", "reconciliationstate")


def _enums_in(schema: str) -> list[str]:
    eng = create_engine(_sync_url())
    try:
        with eng.connect() as c:
            return sorted(c.execute(text(
                "SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
                "WHERE t.typtype = 'e' AND n.nspname = :s"), {"s": schema}).scalars())
    finally:
        eng.dispose()


def _public_tables() -> list[str]:
    eng = create_engine(_sync_url())
    try:
        with eng.connect() as c:
            return sorted(c.execute(text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public'")).scalars())
    finally:
        eng.dispose()


def _exec(sql: str) -> None:
    eng, conn = _connect_autocommit()
    try:
        conn.execute(text(sql))
    finally:
        conn.close()
        eng.dispose()


def test_downgrade_removes_every_enum_the_baseline_owns(empty_db):
    from alembic import command
    cfg = migrate._alembic_config()
    command.upgrade(cfg, "head")
    assert set(OWNED_ENUMS) <= set(_enums_in("public")), "the baseline did not create its enums"
    command.downgrade(cfg, "base")
    left = sorted(set(OWNED_ENUMS) & set(_enums_in("public")))
    assert left == [], f"downgrade left owned enum types behind: {left}"


def test_after_base_the_application_tables_and_version_are_gone(empty_db):
    from alembic import command
    cfg = migrate._alembic_config()
    command.upgrade(cfg, "head")
    assert len(_public_tables()) > 1
    command.downgrade(cfg, "base")
    assert _public_tables() == ["alembic_version"], "application tables survived the downgrade"
    assert _head_in_db() is None, "alembic is not at base"


def test_the_same_database_round_trips_twice(empty_db):
    from alembic import command
    cfg = migrate._alembic_config()
    for cycle in (1, 2):
        command.upgrade(cfg, "head")
        assert _head_in_db() == _alembic_head(), f"cycle {cycle}: upgrade did not reach head"
        command.downgrade(cfg, "base")
        assert _head_in_db() is None, f"cycle {cycle}: downgrade did not reach base"
        assert sorted(set(OWNED_ENUMS) & set(_enums_in("public"))) == [], \
            f"cycle {cycle}: owned enums survived"
    command.upgrade(cfg, "head")
    assert _head_in_db() == _alembic_head()


def test_an_unrelated_enum_in_public_survives(empty_db):
    from alembic import command
    cfg = migrate._alembic_config()
    command.upgrade(cfg, "head")
    _exec("CREATE TYPE public.sentinel_unrelated AS ENUM ('a','b')")
    command.downgrade(cfg, "base")
    assert "sentinel_unrelated" in _enums_in("public"), \
        "the downgrade removed a type it does not own"


def test_an_unrelated_enum_in_another_schema_survives_even_when_identically_named(empty_db):
    from alembic import command
    cfg = migrate._alembic_config()
    command.upgrade(cfg, "head")
    _exec("CREATE SCHEMA IF NOT EXISTS other")
    for name in OWNED_ENUMS:
        _exec(f"DROP TYPE IF EXISTS other.{name}")
        _exec(f"CREATE TYPE other.{name} AS ENUM ('x','y')")
    user = create_engine(_sync_url()).url.username
    _exec(f'ALTER ROLE "{user}" SET search_path = other, public')
    try:
        command.downgrade(cfg, "base")
        assert _enums_in("other") == sorted(OWNED_ENUMS), \
            "the downgrade destroyed another schema's identically named types"
        assert sorted(set(OWNED_ENUMS) & set(_enums_in("public"))) == [], \
            "the downgrade dropped the wrong schema's types and left its own behind"
    finally:
        _exec(f'ALTER ROLE "{user}" RESET search_path')
        _exec("DROP SCHEMA IF EXISTS other CASCADE")


def test_dropping_an_owned_enum_that_is_already_gone_is_harmless(empty_db):
    from alembic import command
    cfg = migrate._alembic_config()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    assert sorted(set(OWNED_ENUMS) & set(_enums_in("public"))) == []
    for name in OWNED_ENUMS:                       # second removal of an absent type
        _exec(f'DROP TYPE IF EXISTS "public"."{name}"')
    command.upgrade(cfg, "head")                   # and the database is still usable
    assert _head_in_db() == _alembic_head()


def test_a_round_tripped_database_matches_a_freshly_upgraded_one(empty_db):
    from alembic import command
    cfg = migrate._alembic_config()

    def _structure() -> list[tuple]:
        eng = create_engine(_sync_url())
        try:
            with eng.connect() as c:
                cols = list(c.execute(text(
                    "SELECT table_name, column_name, data_type, is_nullable, udt_name "
                    "FROM information_schema.columns WHERE table_schema='public' "
                    "ORDER BY table_name, column_name")).all())
                enums = list(c.execute(text(
                    "SELECT t.typname, e.enumlabel FROM pg_type t "
                    "JOIN pg_enum e ON e.enumtypid = t.oid "
                    "JOIN pg_namespace n ON n.oid = t.typnamespace "
                    "WHERE n.nspname='public' ORDER BY t.typname, e.enumsortorder")).all())
            return cols + enums
        finally:
            eng.dispose()

    command.upgrade(cfg, "head")
    direct = _structure()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    assert _structure() == direct, "the re-upgraded schema differs from a direct upgrade"
