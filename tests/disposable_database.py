# Responsibility: Prove from the live server that a database was provisioned for THIS run and may be destroyed.
# Owns: the run identity, the database-side marker, task-database provisioning, and DisposableAuthority.
# Boundaries: it authorises destruction; it never performs a suite's own DDL and never inspects application tables.
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

#: The run identity the supported runner generates once and exports into every process it starts.
RUN_ID_ENV = "MESH_TEST_RUN_ID"
#: The database-side marker. It lives IN the database it describes, so an alias, a rename or a
#: second URL pointing at the same server cannot carry it along - the marker travels with the
#: storage, not with the connection string.
#:
#: It sits in its OWN schema, not `public`, because the first thing the suite does is
#: `DROP SCHEMA public CASCADE` - a marker in `public` would authorise exactly one reset and then
#: be destroyed by it, leaving the database unable to prove itself for the rest of the run.
MARKER_SCHEMA = "_mesh_disposable"
MARKER_TABLE = f"{MARKER_SCHEMA}.run"


# The connected database did not prove it belongs to this run, so nothing may be destroyed.
class NotDisposableError(RuntimeError):
    pass


@dataclass(frozen=True)
class DisposableAuthority:
    # Capability to destroy one specific database. Only `authorize` mints one.
    run_id: str
    database: str
    system_identifier: str
    url: str


def _sync_url(url: URL | str) -> str:
    u = make_url(url)
    if u.drivername.startswith("postgresql+"):
        u = u.set(drivername="postgresql+psycopg2")
    elif u.drivername == "postgresql":
        u = u.set(drivername="postgresql+psycopg2")
    return u.render_as_string(hide_password=False)


def current_run_id() -> str:
    run_id = (os.getenv(RUN_ID_ENV) or "").strip()
    if not run_id:
        raise NotDisposableError(
            f"{RUN_ID_ENV} is not set, so no database can be proven disposable. The supported "
            "runner generates it once per run and provisions a database stamped with it. A test "
            "process that reaches destructive code without it is pointed at a database nobody "
            "provisioned for this run - which is how seven production-shaped rows were lost. "
            "Run `make test-integration` or `make test-container-integration`.")
    return run_id


def task_database_name(run_id: str) -> str:
    # Postgres truncates identifiers at 63 bytes; keep the run id whole and the prefix short.
    return f"meshtest_{run_id}"[:63]


def _server_identity(conn) -> tuple[str, str]:
    database = conn.execute(text("SELECT current_database()")).scalar_one()
    system_identifier = str(
        conn.execute(text("SELECT system_identifier FROM pg_control_system()")).scalar_one())
    return database, system_identifier


# Create a uniquely named database for this run and stamp its marker. Destroys nothing.
def provision(admin_url: URL | str, run_id: str | None = None) -> DisposableAuthority:
    run_id = run_id or current_run_id()
    name = task_database_name(run_id)
    admin = create_engine(_sync_url(admin_url), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            existing = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": name}).scalar()
            if existing:
                raise NotDisposableError(
                    f"database {name!r} already exists. A run identity is used once; reusing one "
                    "would let this run inherit another run's storage.")
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        admin.dispose()

    target = make_url(admin_url).set(database=name)
    engine = create_engine(_sync_url(target), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            database, system_identifier = _server_identity(conn)
            conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {MARKER_SCHEMA}"))
            conn.execute(text(
                f"CREATE TABLE {MARKER_TABLE} ("
                " run_id text PRIMARY KEY,"
                " database_name text NOT NULL,"
                " system_identifier text NOT NULL,"
                " created_at timestamptz NOT NULL DEFAULT now())"))
            conn.execute(
                text(f"INSERT INTO {MARKER_TABLE} (run_id, database_name, system_identifier) "  # noqa: S608 - MARKER_TABLE is a module constant, and every value is bound
                     "VALUES (:r, :d, :s)"),
                {"r": run_id, "d": database, "s": system_identifier})
    finally:
        engine.dispose()
    return DisposableAuthority(
        run_id=run_id, database=name, system_identifier=system_identifier,
        url=make_url(target).render_as_string(hide_password=False))


# Read-only proof that `url` is THIS run's disposable database; raises otherwise. Every question is
# answered by the live server: which database this connection is actually in, which cluster it
# belongs to, and what the marker stored inside it says. No decision is taken from the URL text,
# the database name, the host name, an environment flag or the presence of pytest - the shared
# development database satisfies every one of those too.
def authorize(url: URL | str, run_id: str | None = None) -> DisposableAuthority:
    run_id = run_id or current_run_id()
    engine = create_engine(_sync_url(url), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            database, system_identifier = _server_identity(conn)
            present = conn.execute(text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = :s AND table_name = :t"),
                {"s": MARKER_SCHEMA, "t": "run"}).scalar()
            if not present:
                raise NotDisposableError(
                    f"database {database!r} carries no {MARKER_TABLE} marker, so it was not "
                    "provisioned for this run and must not be reset, truncated or dropped. This "
                    "is what a shared development database looks like.")
            rows = conn.execute(text(
                f"SELECT run_id, database_name, system_identifier FROM {MARKER_TABLE}"  # noqa: S608 - MARKER_TABLE is a module constant
            )).all()
    finally:
        engine.dispose()

    if len(rows) != 1:
        raise NotDisposableError(
            f"database {database!r} holds {len(rows)} run markers; exactly one is required. A "
            "database claimed by more than one run is not disposable by either.")
    marked_run, marked_db, marked_system = rows[0]
    if marked_run != run_id:
        raise NotDisposableError(
            f"database {database!r} is marked for run {marked_run!r}, not this run {run_id!r}. A "
            "marker left behind by an earlier run does not authorise this one.")
    if marked_db != database:
        raise NotDisposableError(
            f"the marker in {database!r} names database {marked_db!r}. The marker was copied from "
            "elsewhere, so it describes storage this connection is not attached to.")
    if marked_system != system_identifier:
        raise NotDisposableError(
            f"the marker in {database!r} names cluster {marked_system!r} but this connection is on "
            f"{system_identifier!r}. The marker was restored from another server.")
    return DisposableAuthority(run_id=run_id, database=database,
                               system_identifier=system_identifier,
                               url=make_url(url).render_as_string(hide_password=False))


# Re-prove an authority against the connection about to be mutated, immediately before DDL.
def require(authority: DisposableAuthority | None, url: URL | str, what: str) -> DisposableAuthority:
    if authority is None:
        raise NotDisposableError(
            f"{what} was called without a DisposableAuthority. Destructive helpers take the "
            "capability as an argument so no caller can forget the check.")
    fresh = authorize(url, run_id=authority.run_id)
    if fresh.database != authority.database or fresh.system_identifier != authority.system_identifier:
        raise NotDisposableError(
            f"{what} holds an authority for {authority.database!r} on cluster "
            f"{authority.system_identifier!r} but is connected to {fresh.database!r} on "
            f"{fresh.system_identifier!r}. Two different databases; the capability does not carry.")
    return fresh


# Remove exactly the database this authority names, after re-proving it.
def drop(authority: DisposableAuthority, admin_url: URL | str) -> None:
    require(authority, authority.url, "drop")
    admin = create_engine(_sync_url(admin_url), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                              "WHERE datname = :d AND pid <> pg_backend_pid()"),
                         {"d": authority.database})
            conn.execute(text(f'DROP DATABASE IF EXISTS "{authority.database}"'))
    finally:
        admin.dispose()


def new_run_id() -> str:
    return uuid.uuid4().hex[:16]
