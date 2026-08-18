# Responsibility: Verify a database is destroyed only when the live server proves it was provisioned for this run.
# Boundaries: the authority and the helpers that require it; the runner's own refusals are shell-level evidence.
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from tests import disposable_database as dd
from tests import harness_provisioning as hp

import meshpipeline.settings.providers as provcfg

pytestmark = pytest.mark.asyncio

if not os.getenv("DATABASE_URL"):
    pytest.skip("a real PostgreSQL endpoint is required", allow_module_level=True)

#: The row a "shared" database holds. Every refusal case asserts it is still there afterwards -
#: a guard that refuses but has already dropped the schema would pass a weaker assertion.
SENTINEL = "_sentinel_rows"


def _admin_url():
    return make_url(provcfg.POSTGRES_DSN).set(database="postgres")


def _engine(url):
    return create_engine(dd._sync_url(url), isolation_level="AUTOCOMMIT")


# A database that looks exactly like a long-lived one: no marker, plausible name, real rows.
def _make_shared_looking(name: str) -> str:
    eng = _engine(_admin_url())
    try:
        with eng.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
            c.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        eng.dispose()
    url = make_url(provcfg.POSTGRES_DSN).set(database=name).render_as_string(hide_password=False)
    eng = _engine(url)
    try:
        with eng.connect() as c:
            c.execute(text(f"CREATE TABLE {SENTINEL} (id int primary key)"))
            c.execute(text(f"INSERT INTO {SENTINEL} VALUES (1), (2), (3)"))
    finally:
        eng.dispose()
    return url


def _sentinel_count(url) -> int:
    eng = _engine(url)
    try:
        with eng.connect() as c:
            return c.execute(text(f"SELECT count(*) FROM {SENTINEL}")).scalar_one()
    finally:
        eng.dispose()


def _destroy(name: str) -> None:
    eng = _engine(_admin_url())
    try:
        with eng.connect() as c:
            c.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                           "WHERE datname = :d AND pid <> pg_backend_pid()"), {"d": name})
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
    finally:
        eng.dispose()


@pytest.fixture()
# A disposable stand-in for the shared development database. Never the real one.
def shared_looking():
    name = f"meshpipeline_shared_{uuid.uuid4().hex[:8]}"
    url = _make_shared_looking(name)
    try:
        yield name, url
    finally:
        _destroy(name)


@pytest.fixture()
# A database provisioned exactly the way the supported runner provisions one.
def task_db(monkeypatch):
    run_id = dd.new_run_id()
    monkeypatch.setenv(dd.RUN_ID_ENV, run_id)
    auth = dd.provision(_admin_url(), run_id)
    try:
        yield auth
    finally:
        _destroy(auth.database)


# the supported path

async def test_the_runner_provisions_a_uniquely_named_database(task_db):
    assert task_db.database == f"meshtest_{task_db.run_id}"
    eng = _engine(_admin_url())
    try:
        with eng.connect() as c:
            assert c.execute(text("SELECT 1 FROM pg_database WHERE datname = :d"),
                             {"d": task_db.database}).scalar() == 1
    finally:
        eng.dispose()


async def test_the_database_side_marker_matches_the_run(task_db):
    proved = dd.authorize(task_db.url, task_db.run_id)
    assert proved.run_id == task_db.run_id
    assert proved.database == task_db.database
    assert proved.system_identifier == task_db.system_identifier


async def test_a_destructive_reset_succeeds_on_the_authorized_database(task_db):
    await hp.reset_schema(task_db.url, authority=task_db)
    assert "simulation_jobs" in await hp.table_names(
        make_url(task_db.url).set(drivername="postgresql+asyncpg"))


async def test_the_authorized_database_completes_a_fixture_cycle(task_db):
    await hp.reset_schema(task_db.url, authority=task_db)
    await hp.truncate_tables(task_db.url, "simulation_jobs", authority=task_db)
    await hp.drop_tables(task_db.url, "does_not_exist_here", authority=task_db)


async def test_migrations_run_both_ways_against_an_authorized_database(task_db):
    from alembic import command

    from meshpipeline.runtime import migrate
    saved = (provcfg.DATABASE_URL, provcfg.POSTGRES_DSN)
    provcfg.DATABASE_URL = provcfg.POSTGRES_DSN = task_db.url
    try:
        hp.upgrade_to_head(task_db.url)
        command.downgrade(migrate._alembic_config(), "base")
        assert "simulation_jobs" not in await hp.table_names(
            make_url(task_db.url).set(drivername="postgresql+asyncpg"))
        command.upgrade(migrate._alembic_config(), "head")
        assert "simulation_jobs" in await hp.table_names(
            make_url(task_db.url).set(drivername="postgresql+asyncpg"))
    finally:
        provcfg.DATABASE_URL, provcfg.POSTGRES_DSN = saved


async def test_the_task_database_is_removed_afterwards():
    run_id = dd.new_run_id()
    os.environ[dd.RUN_ID_ENV] = run_id
    try:
        auth = dd.provision(_admin_url(), run_id)
        dd.drop(auth, _admin_url())
        eng = _engine(_admin_url())
        try:
            with eng.connect() as c:
                assert c.execute(text("SELECT 1 FROM pg_database WHERE datname = :d"),
                                 {"d": auth.database}).scalar() is None
        finally:
            eng.dispose()
    finally:
        os.environ.pop(dd.RUN_ID_ENV, None)


# refusals

async def test_an_unmarked_shared_database_is_refused(shared_looking, monkeypatch):
    _name, url = shared_looking
    monkeypatch.setenv(dd.RUN_ID_ENV, dd.new_run_id())
    with pytest.raises(dd.NotDisposableError, match="carries no"):
        dd.authorize(url)
    assert _sentinel_count(url) == 3


async def test_a_database_named_test_is_refused_without_a_marker(monkeypatch):
    name = f"meshpipeline_test_{uuid.uuid4().hex[:8]}"
    url = _make_shared_looking(name)
    monkeypatch.setenv(dd.RUN_ID_ENV, dd.new_run_id())
    try:
        with pytest.raises(dd.NotDisposableError):
            dd.authorize(url)
        assert _sentinel_count(url) == 3
    finally:
        _destroy(name)


@pytest.mark.parametrize("flag,value", [
    ("ALLOW_RESET", "1"), ("ALLOW_DB_RESET", "true"), ("FORCE_RESET", "yes"),
    ("CI", "true"), ("PYTEST_CURRENT_TEST", "test_x (call)")])
async def test_no_environment_flag_authorizes_destruction(shared_looking, monkeypatch, flag, value):
    _name, url = shared_looking
    monkeypatch.setenv(dd.RUN_ID_ENV, dd.new_run_id())
    monkeypatch.setenv(flag, value)
    with pytest.raises(dd.NotDisposableError):
        dd.authorize(url)
    assert _sentinel_count(url) == 3


async def test_a_url_alias_to_the_same_database_is_still_refused(shared_looking, monkeypatch):
    name, url = shared_looking
    monkeypatch.setenv(dd.RUN_ID_ENV, dd.new_run_id())
    import socket

    u = make_url(url)
    # The SAME server reached by a textually different URL: its own address. A guard that compares
    # connection strings sees two databases here; a guard that asks the server sees one.
    alias_host = socket.gethostbyname(u.host)
    if alias_host == u.host:
        pytest.skip("the host is already an address; no distinct alias exists here")
    alias = u.set(host=alias_host).render_as_string(hide_password=False)
    assert alias != url, "the alias must differ textually or it proves nothing"
    with pytest.raises(dd.NotDisposableError):
        dd.authorize(alias)
    assert _sentinel_count(url) == 3


async def test_a_marker_from_a_previous_run_is_refused(task_db, monkeypatch):
    monkeypatch.setenv(dd.RUN_ID_ENV, dd.new_run_id())      # a NEW run, same database
    with pytest.raises(dd.NotDisposableError, match="is marked for run"):
        dd.authorize(task_db.url)


async def test_a_mismatched_run_identity_is_refused(task_db):
    with pytest.raises(dd.NotDisposableError, match="is marked for run"):
        dd.authorize(task_db.url, run_id=dd.new_run_id())


async def test_a_missing_run_identity_is_refused(shared_looking, monkeypatch):
    _name, url = shared_looking
    monkeypatch.delenv(dd.RUN_ID_ENV, raising=False)
    with pytest.raises(dd.NotDisposableError, match=dd.RUN_ID_ENV):
        dd.authorize(url)
    assert _sentinel_count(url) == 3


# the refusal happens BEFORE anything is destroyed

async def test_every_destructive_helper_refuses_before_it_mutates(shared_looking, monkeypatch):
    _name, url = shared_looking
    monkeypatch.setenv(dd.RUN_ID_ENV, dd.new_run_id())
    async_url = make_url(url).set(drivername="postgresql+asyncpg").render_as_string(
        hide_password=False)

    with pytest.raises(dd.NotDisposableError):
        await hp.reset_schema(async_url, authority=None)
    with pytest.raises(dd.NotDisposableError):
        await hp.truncate_tables(async_url, SENTINEL, authority=None)
    with pytest.raises(dd.NotDisposableError):
        await hp.drop_tables(async_url, SENTINEL, authority=None)

    # the sentinel table AND its rows survive all three
    assert _sentinel_count(url) == 3


async def test_an_authority_for_another_database_does_not_carry(task_db, shared_looking):
    _name, url = shared_looking
    async_url = make_url(url).set(drivername="postgresql+asyncpg").render_as_string(
        hide_password=False)
    with pytest.raises(dd.NotDisposableError):
        await hp.reset_schema(async_url, authority=task_db)
    assert _sentinel_count(url) == 3
