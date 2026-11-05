# Responsibility: Bring a database to the shipped schema, once, safely, whoever starts first.
# Owns: the advisory-locked upgrade and three refusals: unmanaged schema, unshipped revision, incomplete schema.
# Boundaries: it migrates or it refuses.
# Collaborates with: alembic/env.py and persistence/migration_url.py.
from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import urlsplit

import meshpipeline.settings.policy as polcfg
import meshpipeline.settings.providers as provcfg
from meshpipeline.persistence.migration_url import sync_migration_url

logger = logging.getLogger(__name__)

# A fixed 32-bit key so every instance contends on the SAME advisory lock (pg_advisory_lock takes
# a signed 64-bit key; this value is comfortably in range). "mesh".
_LOCK_KEY = 0x6D657368
_ACQUIRE_TIMEOUT_SECONDS = 120

# Hosts that mean "the local Compose stack", never the hosted database.
_LOCAL_DB_HOSTS = {"", "localhost", "127.0.0.1", "::1", "postgres", "db", "host.docker.internal"}


def _migration_source() -> str:
    return provcfg.DATABASE_URL or provcfg.POSTGRES_DSN


def _guard_not_local_db_in_production(dsn: str, env_name: str) -> None:
    host = (urlsplit(dsn).hostname or "").lower()
    if env_name == "production" and host in _LOCAL_DB_HOSTS:
        raise SystemExit(
            f"refusing to migrate: ENV=production but DATABASE_URL points at a local host "
            f"('{host or '<none>'}'). A production run must target its own database, "
            "not the local Docker Compose Postgres - check DATABASE_URL.")


def _script_location_from_ini(ini: Path) -> str:
    from configparser import ConfigParser
    cp = ConfigParser()
    cp.read(str(ini))
    loc = Path(cp.get("alembic", "script_location", fallback="alembic"))
    return str(loc if loc.is_absolute() else (ini.resolve().parent / loc))


def _resolve_alembic_assets() -> tuple[str, str]:
    from meshpipeline.settings.env import optional_env
    override = optional_env("ALEMBIC_CONFIG", "")
    if override:
        p = Path(override)
        if not p.is_file():
            raise SystemExit(f"ALEMBIC_CONFIG={override!r} is not a file")
        sibling = p.resolve().parent / "alembic"
        return str(p), (str(sibling) if sibling.is_dir() else _script_location_from_ini(p))

    for root in (Path("/srv"), Path(__file__).resolve().parents[3]):
        ini, scripts = root / "alembic.ini", root / "alembic"
        if ini.is_file() and scripts.is_dir():
            return str(ini), str(scripts)

    raise SystemExit(
        "no trusted Alembic configuration found. alembic.ini + alembic/ are deployment assets: "
        "they ship in the API/pipeline image at /srv and exist in a repository checkout. The "
        "current working directory is deliberately NOT searched - set ALEMBIC_CONFIG to an "
        "explicit path to migrate from an environment that has neither.")


def _alembic_config(*, configure_logging: bool = True):
    from alembic.config import Config
    ini, script_location = _resolve_alembic_assets()
    cfg = Config(ini)
    # `alembic/env.py` calls `fileConfig`, which reconfigures the ROOT logger for the whole
    # process. That is right for the migration CLI, which owns its process, and wrong for a
    # caller that drives migrations in-process - it would silently restyle every other logger.
    cfg.attributes["configure_logger"] = configure_logging
    # Absolute script directory, so `upgrade` finds env.py and versions/ regardless of cwd.
    cfg.set_main_option("script_location", script_location)
    return cfg


class UnmanagedSchemaError(RuntimeError):
    pass


class UnknownRevisionError(RuntimeError):
    pass


class IncompleteSchemaError(RuntimeError):
    pass


#: Objects only THIS application creates. Recognising a Hexera installation means finding
#: one of these - not "the schema is non-empty", which would refuse a database we happen to share
#: with something else. Listed explicitly rather than derived, so an unrelated table named
#: `artifacts` in somebody else's product cannot be mistaken for ours by a pattern.
_OWNED_TABLES = frozenset({
    "simulation_jobs", "chat_sessions", "artifacts", "artifact_reconciliations",
    "terminal_outbox", "geometry_sources", "capture_operations", "geometry_interpretations",
    "native_submission_claims", "source_object_cleanups",
})
_OWNED_TYPES = frozenset({"jobstatus", "failedreason", "artifacttype", "reconciliationstate"})
_OWNED_FUNCTIONS = frozenset({"_chat_sessions_set_updated_at"})


#: A schema name we are willing to put into SQL. The same rule `alembic/env.py` applies before it
#: builds DDL from this value: Alembic's configuration is a deployment value rather than user
#: input, but it still reaches SQL as an identifier, so it is validated instead of trusted.
_SAFE_SCHEMA = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def intended_schema() -> str:
    schema = (_alembic_config().get_main_option("version_table_schema") or "public").strip()
    if not _SAFE_SCHEMA.match(schema):
        raise SystemExit(
            f"version_table_schema={schema!r} is not a plain PostgreSQL identifier; refusing to "
            "build SQL from it")
    return schema


def _existing_objects(conn, schema: str) -> dict:
    from sqlalchemy import text

    def names(sql: str) -> set[str]:
        return {r[0] for r in conn.execute(text(sql), {"s": schema}).all()}

    return {
        "tables": names("SELECT c.relname FROM pg_class c JOIN pg_namespace n "
                        "ON n.oid = c.relnamespace WHERE n.nspname = :s AND c.relkind = 'r'"),
        "types": names("SELECT t.typname FROM pg_type t JOIN pg_namespace n "
                       "ON n.oid = t.typnamespace WHERE n.nspname = :s AND t.typtype = 'e'"),
        "functions": names("SELECT p.proname FROM pg_proc p JOIN pg_namespace n "
                           "ON n.oid = p.pronamespace WHERE n.nspname = :s"),
    }


def _safe_location(dsn: str) -> str:
    from sqlalchemy.engine import make_url

    url = make_url(dsn)
    return f"{url.host or 'localhost'}:{url.port or 5432}/{url.database or '?'}"


def assert_schema_is_managed(conn, dsn: str) -> None:
    schema = intended_schema()
    present = _existing_objects(conn, schema)
    if "alembic_version" in present["tables"]:
        return                                    # managed: Alembic owns this database

    found = sorted(
        [f"table {t}" for t in sorted(present["tables"] & _OWNED_TABLES)]
        + [f"type {t}" for t in sorted(present["types"] & _OWNED_TYPES)]
        + [f"function {f}" for f in sorted(present["functions"] & _OWNED_FUNCTIONS)]
    )
    if not found:
        return                                    # empty, or only objects that are not ours

    raise UnmanagedSchemaError(
        f"schema {schema!r} at {_safe_location(dsn)} already contains Hexera objects "
        f"({', '.join(found[:6])}{'...' if len(found) > 6 else ''}) but has no alembic_version "
        "table, so no revision describes what is in it. Refusing to migrate; the database has "
        "not been modified. This pre-release build ships one baseline and no adoption path: "
        "recreate the database empty and let the baseline build it. Do not stamp the baseline "
        "onto this schema - that asserts a shape nobody has verified and makes the mismatch "
        "permanent and silent."
    )


def assert_stamp_is_known(conn, dsn: str) -> None:
    from sqlalchemy import text

    schema = intended_schema()
    if "alembic_version" not in _existing_objects(conn, schema)["tables"]:
        return                                    # fresh install: nothing is stamped yet

    # The schema is validated as a plain identifier by `intended_schema()` and quoted here; an
    # identifier cannot be a bound parameter.
    stamped = {r[0] for r in conn.execute(
        text(f'SELECT version_num FROM "{schema}".alembic_version')).all()}  # noqa: S608
    if not stamped:
        return                                    # a version table with no row: Alembic's own

    from alembic.script import ScriptDirectory
    known = {s.revision for s in ScriptDirectory.from_config(
        _alembic_config(configure_logging=False)).walk_revisions()}
    unknown = sorted(stamped - known)
    if not unknown:
        return

    raise UnknownRevisionError(
        f"schema {schema!r} at {_safe_location(dsn)} is stamped with revision(s) "
        f"{', '.join(repr(u) for u in unknown)}, which this build does not ship. The migration "
        f"history is a single initial-schema baseline ({', '.join(sorted(known))}); the "
        "superseded development revisions were removed and there is no upgrade path from them. "
        "The database has not been modified. Recreate it against an empty database - no "
        "Hexera deployment has launched, so no data here is expected to survive. Do not "
        "restamp it to the baseline: that asserts the schema matches a revision nobody has "
        "verified, which makes the mismatch permanent and silent."
    )


def assert_schema_is_complete(conn, dsn: str) -> None:
    # A stamp is a claim about the schema, not the schema itself. This repository ships ONE rooted
    # revision that is edited in place, so a database stamped with it can predate an object the
    # current baseline defines: `upgrade head` sees the stamp, does nothing, and start-up would
    # continue against a schema that is missing tables. Compared against the shipped metadata -
    # the same models the baseline is authored from - rather than a hand-kept list that would
    # drift from it.
    from meshpipeline.persistence.models import Base

    schema = intended_schema()
    present = _existing_objects(conn, schema)["tables"]
    missing = sorted(name for name in Base.metadata.tables if name not in present)
    if not missing:
        return

    raise IncompleteSchemaError(
        f"schema {schema!r} at {_safe_location(dsn)} is stamped with a revision this build "
        f"ships, but {len(missing)} table(s) it defines are absent "
        f"({', '.join(missing[:6])}{'...' if len(missing) > 6 else ''}). That happens when a "
        "database was created from an earlier form of the single baseline revision, so no "
        "upgrade step remains to add them. Refusing to start against an incomplete schema; the "
        "database has not been modified. Recreate it empty and start again - "
        "`docker compose down -v` then `docker compose up` rebuilds it from the shipped "
        "baseline. Do not create the missing objects by hand: that leaves the stamp asserting a "
        "schema nobody has verified."
    )


def _alembic_upgrade_head(*, configure_logging: bool = True) -> None:
    from alembic import command
    command.upgrade(_alembic_config(configure_logging=configure_logging), "head")


def _run_locked(conn, upgrade, key: int = _LOCK_KEY,
                timeout_seconds: int = _ACQUIRE_TIMEOUT_SECONDS) -> None:
    from sqlalchemy import text
    conn.execute(text(f"SET statement_timeout = '{int(timeout_seconds)}s'"))
    try:
        conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
    except Exception as exc:   # the wait was cancelled by statement_timeout, or the DB is down
        raise SystemExit(
            f"could not acquire the migration lock within {timeout_seconds}s "
            f"(another instance may be stuck migrating): {exc}") from exc
    conn.execute(text("RESET statement_timeout"))     # do not let the timeout kill a long migration
    try:
        upgrade()
    finally:
        conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})


def run_migrations(*, dsn: str | None = None, env_name: str | None = None,
                   configure_logging: bool = True) -> None:
    source = dsn or _migration_source()
    env_name = (env_name or polcfg.ENV).lower()
    _guard_not_local_db_in_production(source, env_name)

    # The advisory-lock connection uses the SAME sync URL alembic/env.py builds (one source of
    # truth): parsed, driver-swapped to psycopg2, TLS made explicit. Never logged.
    sync_url = sync_migration_url(source)

    from sqlalchemy import create_engine
    engine = create_engine(sync_url, pool_pre_ping=True)
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            def _guarded_upgrade() -> None:
                # Inside the advisory lock and before any DDL: two instances starting together
                # reach the same verdict, and a refusal happens while the database is untouched.
                assert_schema_is_managed(conn, sync_url)
                assert_stamp_is_known(conn, sync_url)
                _alembic_upgrade_head(configure_logging=configure_logging)
                # AFTER the upgrade: a stamp that already reads `head` skips every step, so the
                # only place an incomplete schema is visible is once there is nothing left to run.
                assert_schema_is_complete(conn, sync_url)

            _run_locked(conn, _guarded_upgrade)
    finally:
        engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    logger.info("running database migrations (advisory-locked)…")
    try:
        run_migrations()
    except (UnmanagedSchemaError, UnknownRevisionError, IncompleteSchemaError) as exc:
        # Both are refusals an operator has to act on - recreate the database, or adopt it
        # deliberately - not defects in this process. The message already says what to do; a
        # traceback above it only buries it. The exit code still fails the container start.
        raise SystemExit(str(exc)) from None
    logger.info("database migrations complete")


if __name__ == "__main__":
    main()
