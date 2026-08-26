# Responsibility: Verify the per-loop engine leaves no stranded server connections across task loops,
# and that crash finalize recovers checkpoint facts through a fresh saver, against real PostgreSQL.
# Boundaries: connection lifecycle + the crash-facts ladder; fence semantics live in their own files.
from __future__ import annotations

import asyncio
import logging
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import meshpipeline.settings.providers as provcfg
from meshpipeline.persistence.models import SimulationJob

pytestmark = pytest.mark.asyncio
if not os.getenv("DATABASE_URL"):
    pytest.skip("a real PostgreSQL endpoint is required", allow_module_level=True)

jlog = logging.getLogger(__name__)


def _sessions():
    engine = create_async_engine(provcfg.POSTGRES_DSN, pool_size=4, max_overflow=4,
                                 pool_pre_ping=True)
    return engine, async_sessionmaker(bind=engine, expire_on_commit=False)


async def _activity_count() -> int:
    engine, Session = _sessions()
    try:
        async with Session() as db:
            return int((await db.execute(text(
                "select count(*) from pg_stat_activity "
                "where datname = current_database()"))).scalar_one())
    finally:
        await engine.dispose()


def test_global_engine_connection_census():
    # Twenty task-shaped loops each touch the process-global engine surface (get_db), then run
    # the worker's eager dispose - the pg_stat_activity census must return to baseline. Bare
    # dispose(close=False) without the eager path strands one server connection per loop.
    import meshpipeline.persistence.session as sess

    sess.reset_session_state()
    baseline = asyncio.run(_activity_count())

    async def _task_body():
        async with sess.get_db() as db:
            await db.execute(text("select 1"))
        await sess.dispose_engine()          # the worker's eager per-loop cleanup

    for _ in range(20):
        asyncio.run(_task_body())

    after = asyncio.run(_activity_count())
    assert after <= baseline + 2, (
        f"{after - baseline} server connections stranded after 20 task loops")


async def test_crash_finalize_recovers_checkpoint_facts_after_saver_closed():
    # The run's saver lives in an async-with that every crash unwinds BEFORE the handler runs,
    # so the graph binding's read always fails "the connection is closed". The ladder's fresh
    # saver must recover the checkpoint's durable facts instead of approved-intent-only.
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from meshpipeline.application.terminal_finalize import durable_facts_after_crash
    from meshpipeline.persistence.repositories.job_repository import JobRepository
    from meshpipeline.pipeline.graph import build_graph

    engine, Session = _sessions()
    job_id = uuid.uuid4()
    try:
        async with Session() as db:
            db.add(SimulationJob(id=job_id, owner_id=f"crash-{job_id.hex[:8]}"))
            await db.commit()

        dsn = provcfg.POSTGRES_DSN.replace("+asyncpg", "").replace("+psycopg", "")
        config = {"configurable": {"thread_id": str(job_id)}}
        async with AsyncPostgresSaver.from_conn_string(dsn) as cp:
            await cp.setup()
            graph = build_graph(checkpointer=cp)
            await graph.aupdate_state(config, {
                "engine": "snappy", "purpose": "external_cfd", "dimensionality": "3D",
                "retry_count": 2})
        # the async-with has unwound: the saver's connection is closed, the binding is dead

        facts = await durable_facts_after_crash(
            Session, job_id=str(job_id), approved={"mesh_engine": "cfmesh"},
            graph=graph, graph_config=config, job_repo=JobRepository(), jlog=jlog,
            used_durable_checkpointer=True)
        assert facts["engine"] == "snappy", "crash facts fell back to approved intent"
        assert facts["attempts"] == 2
        assert facts["purpose"] == "external_cfd"
    finally:
        await engine.dispose()
