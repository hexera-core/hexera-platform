# Responsibility: Verify a superseded worker cannot write a checkpoint, while reads stay unfenced so resume works.
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests import harness_provisioning as hp

import meshpipeline.settings.providers as provcfg
from meshpipeline.application import execution_fence as fence
from meshpipeline.application.fenced_checkpointer import FencedCheckpointer
from meshpipeline.contracts.pipeline_state import STATE_SCHEMA_VERSION
from meshpipeline.persistence.lease import ClaimResult, LeaseRepository
from meshpipeline.persistence.models import JobStatus, SimulationJob

lease = LeaseRepository()
pytestmark = pytest.mark.usefixtures("SessionLocal")


@pytest.fixture()
async def SessionLocal():
    connect_args = {"ssl": True} if provcfg.DB_SSL_REQUIRED else {}
    engine = create_async_engine(provcfg.POSTGRES_DSN, connect_args=connect_args, pool_size=8)
    # Alembic is the only thing that creates this schema - see
    # tests/harness_provisioning.py. `create_all` built tables no migration
    # had produced, so a suite could pass against a schema production never has.
    await hp.reset_schema(provcfg.POSTGRES_DSN)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _checkpoint_state(jid) -> dict[str, set]:
    # EXACT identities, not counts: a stale write that replaced or moved a row would keep the
    # count identical. Scoped to every thread this job owns, across generations and namespaces.
    import psycopg
    like = f"{jid}:%"
    with psycopg.connect(_pg_dsn()) as c, c.cursor() as cur:
        cur.execute("select thread_id, checkpoint_ns, checkpoint_id, md5(checkpoint::text) "
                    "from checkpoints where thread_id like %s", (like,))
        cps = set(cur.fetchall())
        cur.execute("select thread_id, checkpoint_ns, channel, version "
                    "from checkpoint_blobs where thread_id like %s", (like,))
        blobs = set(cur.fetchall())
        cur.execute("select thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel "
                    "from checkpoint_writes where thread_id like %s", (like,))
        writes = set(cur.fetchall())
    return {"checkpoints": cps, "blobs": blobs, "writes": writes}


def _pg_dsn() -> str:
    return provcfg.POSTGRES_DSN.replace("+asyncpg", "").replace("+psycopg", "")


async def _claimed(SessionLocal, exec_id="exec-A"):
    async with SessionLocal() as s:
        job = SimulationJob(owner_id="o", status=JobStatus.pending)
        s.add(job)
        await s.commit()
        jid = job.id
    async with SessionLocal() as s:
        res, own = await lease.claim_execution(
            s, jid, worker_token=uuid.uuid4(), backend="celery", backend_execution_id=exec_id)
        await s.commit()
    assert res == ClaimResult.acquired_new_generation
    return jid, own


def _cfg(job_id, generation):
    return {"configurable": {"thread_id": f"{job_id}:s{STATE_SCHEMA_VERSION}:g{generation}",
                             "checkpoint_ns": ""}}


def _checkpoint(cid: str):
    return {"v": 1, "id": cid, "ts": "2026-07-24T00:00:00+00:00",
            "channel_values": {"stage": cid}, "channel_versions": {"stage": 1},
            "versions_seen": {}}


async def _saver():
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    return AsyncPostgresSaver.from_conn_string(_pg_dsn())


# the current owner writes and can read back
async def test_current_owner_writes_and_resumes(SessionLocal):
    jid, own = await _claimed(SessionLocal)
    async with await _saver() as inner:
        await inner.setup()
        ck = FencedCheckpointer(inner, job_id=str(jid),
                                execution_generation=own.execution_generation,
                                approved_intent_fingerprint="fp-1",
                                state_schema_version=STATE_SCHEMA_VERSION)
        cfg = _cfg(jid, own.execution_generation)
        with fence.execution_ownership(own, session_factory=SessionLocal):
            await ck.aput(cfg, _checkpoint("after-planner"), {"source": "loop", "step": 1}, {})
        got = await ck.aget_tuple(cfg)                    # READ is unfenced by design
        assert got is not None
        assert got.checkpoint["channel_values"]["stage"] == "after-planner"


# a superseded worker cannot write a checkpoint
async def test_a_superseded_worker_cannot_write_a_checkpoint(SessionLocal):
    jid, own = await _claimed(SessionLocal)
    async with await _saver() as inner:
        await inner.setup()
        ck = FencedCheckpointer(inner, job_id=str(jid),
                                execution_generation=own.execution_generation,
                                approved_intent_fingerprint="fp-1",
                                state_schema_version=STATE_SCHEMA_VERSION)
        cfg = _cfg(jid, own.execution_generation)
        with fence.execution_ownership(own, session_factory=SessionLocal):
            await ck.aput(cfg, _checkpoint("owned"), {"source": "loop", "step": 1}, {})
        # a different execution takes over
        async with SessionLocal() as s:
            row = await s.get(SimulationJob, jid)
            row.execution_generation += 1
            row.active_worker_token = uuid.uuid4()
            await s.commit()
        before = _checkpoint_state(jid)
        with fence.execution_ownership(own, session_factory=SessionLocal):
            with pytest.raises(fence.StaleWorkerFenced):
                await ck.aput(cfg, _checkpoint("stale"), {"source": "loop", "step": 2}, {})
            with pytest.raises(fence.StaleWorkerFenced):
                await ck.aput_writes(cfg, [("stage", "stale")], "task-1")
        # the stale write did NOT land - the last accepted checkpoint is still the owner's
        got = await ck.aget_tuple(cfg)
        assert got.checkpoint["channel_values"]["stage"] == "owned"
        # and nothing durable was inserted, replaced or moved by either stale attempt
        after = _checkpoint_state(jid)
        for table in ("checkpoints", "blobs", "writes"):
            assert after[table] == before[table], (
                f"the stale attempt changed {table}: "
                f"added={after[table] - before[table]} removed={before[table] - after[table]}")


# reads stay available so a legitimate restart can resume
async def test_reads_are_not_fenced_so_resume_still_works(SessionLocal):
    jid, own = await _claimed(SessionLocal)
    async with await _saver() as inner:
        await inner.setup()
        ck = FencedCheckpointer(inner, job_id=str(jid),
                                execution_generation=own.execution_generation)
        cfg = _cfg(jid, own.execution_generation)
        with fence.execution_ownership(own, session_factory=SessionLocal):
            await ck.aput(cfg, _checkpoint("resumable"), {"source": "loop", "step": 1}, {})
        async with SessionLocal() as s:                    # take the lease away entirely
            row = await s.get(SimulationJob, jid)
            row.execution_generation += 1
            row.active_worker_token = uuid.uuid4()
            await s.commit()
        with fence.execution_ownership(own, session_factory=SessionLocal):
            got = await ck.aget_tuple(cfg)                 # still readable
        assert got.checkpoint["channel_values"]["stage"] == "resumable"


# checkpoint metadata is self-describing
async def test_checkpoint_metadata_carries_the_run_identity(SessionLocal):
    jid, own = await _claimed(SessionLocal)
    async with await _saver() as inner:
        await inner.setup()
        ck = FencedCheckpointer(inner, job_id=str(jid),
                                execution_generation=own.execution_generation,
                                approved_intent_fingerprint="fp-abc",
                                state_schema_version=STATE_SCHEMA_VERSION)
        cfg = _cfg(jid, own.execution_generation)
        with fence.execution_ownership(own, session_factory=SessionLocal):
            await ck.aput(cfg, _checkpoint("m"),
                          {"source": "loop", "step": 3,
                           "writes": {"node_builder": {"retry_count": 2}}}, {})
        got = await ck.aget_tuple(cfg)
        ident = got.metadata["meshpipeline"]
        assert ident["job_id"] == str(jid)
        assert ident["execution_generation"] == own.execution_generation
        assert ident["approved_intent_fingerprint"] == "fp-abc"
        assert ident["state_schema_version"] == STATE_SCHEMA_VERSION
        assert ident["attempt"] == 2                       # the logical builder attempt
        assert got.metadata["step"] == 3                   # LangGraph's own metadata is preserved


# a new generation reads a different thread (namespacing)
async def test_a_new_generation_does_not_see_the_old_generations_checkpoint(SessionLocal):
    jid, own = await _claimed(SessionLocal)
    async with await _saver() as inner:
        await inner.setup()
        ck = FencedCheckpointer(inner, job_id=str(jid),
                                execution_generation=own.execution_generation)
        with fence.execution_ownership(own, session_factory=SessionLocal):
            await ck.aput(_cfg(jid, own.execution_generation), _checkpoint("gen1"),
                          {"source": "loop", "step": 1}, {})
        assert await ck.aget_tuple(_cfg(jid, own.execution_generation + 1)) is None


# the wrapper still reports the effective saver class readiness)
async def test_the_effective_checkpointer_class_is_reportable(SessionLocal):
    async with await _saver() as inner:
        ck = FencedCheckpointer(inner, job_id="j", execution_generation=1)
        assert type(ck.inner).__name__ == "AsyncPostgresSaver"
        assert hasattr(ck, "aget_tuple")                   # delegation keeps the saver surface
