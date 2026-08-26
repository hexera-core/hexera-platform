# Responsibility: Verify the harness builds the declared schema from nothing, idempotently, preserving existing rows.
from __future__ import annotations

import os
import uuid

import pytest
from tests import harness_provisioning as hp

if not os.getenv("DATABASE_URL"):
    pytest.skip("a real PostgreSQL endpoint is required", allow_module_level=True)

pytestmark = pytest.mark.asyncio


async def test_every_declared_table_exists_on_the_live_stack():
    from meshpipeline.persistence.models import Base

    present = await hp.table_names(hp.dsn())
    declared = set(Base.metadata.tables)

    assert declared, "no tables declared - the model import is the thing that broke"
    assert not (declared - present), (
        f"missing on a live stack: {sorted(declared - present)}. Harness provisioning did not run, "
        "so which suites pass now depends on which suite ran first."
    )


@pytest.fixture()
async def empty_database():
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    admin = hp.dsn()
    name = f"provisioning_probe_{uuid.uuid4().hex[:12]}"
    engine = create_async_engine(admin, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f'create database "{name}"'))
        yield admin.set(database=name)
        async with engine.connect() as conn:
            await conn.execute(
                text("select pg_terminate_backend(pid) from pg_stat_activity "
                     "where datname = :n and pid <> pg_backend_pid()"), {"n": name})
            await conn.execute(text(f'drop database if exists "{name}"'))
    finally:
        await engine.dispose()


async def test_provisioning_builds_the_schema_from_nothing(empty_database):
    from meshpipeline.persistence.models import Base

    assert await hp.table_names(empty_database) == set(), "the probe database was not empty"

    await hp.ensure_schema(empty_database)

    created = await hp.table_names(empty_database)
    assert not (set(Base.metadata.tables) - created), (
        f"provisioning left out: {sorted(set(Base.metadata.tables) - created)}"
    )


async def test_provisioning_is_idempotent_and_preserves_existing_rows(empty_database):
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from meshpipeline.persistence.models import GeometrySource

    await hp.ensure_schema(empty_database)
    after_first = await hp.table_names(empty_database)

    row_id = uuid.uuid4()
    engine = create_async_engine(empty_database)
    try:
        async with AsyncSession(engine) as db:
            db.add(GeometrySource(
                id=row_id, owner_id=str(uuid.uuid4()), original_filename="probe.step",
                suffix_hint=".step", object_key=f"sources/{row_id}/probe.step",
                sha256="d" * 64, size_bytes=4))
            await db.commit()

        await hp.ensure_schema(empty_database)  # what the next session's provisioning would do

        async with AsyncSession(engine) as db:
            surviving = (await db.execute(
                select(func.count()).select_from(GeometrySource))).scalar_one()
    finally:
        await engine.dispose()

    assert await hp.table_names(empty_database) == after_first, "re-provisioning changed the schema"
    assert surviving == 1, "re-provisioning destroyed data an earlier suite had written"


# the engine must be released between loops

def test_a_second_event_loop_needs_the_engine_released_first():
    import asyncio

    from sqlalchemy import text

    from meshpipeline.persistence.session import dispose_engine, get_db

    async def _query_then_release():
        try:
            async with get_db() as db:
                return (await db.execute(text("select 1"))).scalar_one()
        finally:
            await dispose_engine()

    assert asyncio.run(_query_then_release()) == 1
    assert asyncio.run(_query_then_release()) == 1     # a loop the first engine never knew


def test_run_from_job_releases_the_engine_before_opening_its_second_loop():
    import asyncio

    import meshpipeline.persistence.session as sess
    from meshpipeline.application.pipeline_run import run_from_job

    seen: dict = {}

    def _capture(**kwargs):
        # runs after the first loop has closed - exactly where a stale pool would be waiting
        with sess._engines_lock:
            seen["engines_after_first_loop"] = len(sess._engines)
        return {"ok": True}

    import meshpipeline.application.pipeline_run as pr
    sess.reset_session_state()
    original = pr.run_pipeline
    pr.run_pipeline = _capture
    try:
        with pytest.raises(SystemExit):
            run_from_job(str(uuid.uuid4()))        # no such job: raises AFTER the loop ran
        asyncio.run(_noop())                       # a second loop must not inherit anything
    finally:
        pr.run_pipeline = original

    # A missing job exits during the FIRST loop, so the capture may never run; when it does
    # run, the first loop's engine must already be gone.
    if "engines_after_first_loop" in seen:
        assert seen["engines_after_first_loop"] == 0, (
            "run_from_job left an engine cached for its first loop; the pipeline's own loop "
            "would inherit a connection it cannot await")
    with sess._engines_lock:
        remaining = len(sess._engines)
    assert remaining == 0, "an engine survived past run_from_job's loops"


async def _noop():
    return None
