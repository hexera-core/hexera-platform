# Responsibility: Verify a cancelled owner writes nothing durable, keeps its claim, and hands the job to nobody.
from __future__ import annotations

import asyncio
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
    await hp.reset_schema(provcfg.POSTGRES_DSN)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _pg_dsn() -> str:
    return provcfg.POSTGRES_DSN.replace("+asyncpg", "").replace("+psycopg", "")


def _cfg(job_id, generation):
    return {"configurable": {"thread_id": f"{job_id}:s{STATE_SCHEMA_VERSION}:g{generation}",
                             "checkpoint_ns": ""}}


def _checkpoint(cid: str):
    return {"v": 1, "id": cid, "ts": "2026-08-03T00:00:00+00:00",
            "channel_values": {"stage": cid}, "channel_versions": {"stage": 1},
            "versions_seen": {}}


async def _saver():
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    return AsyncPostgresSaver.from_conn_string(_pg_dsn())


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


async def _owner_row(SessionLocal, jid):
    async with SessionLocal() as s:
        row = await s.get(SimulationJob, jid)
        return row.execution_generation, row.active_worker_token, row.status


# the loop, cancelled

def _loop_pieces(probe_records: list, *, hang: asyncio.Event):
    from meshpipeline.agents.loop.driver import RoundDecision
    from meshpipeline.contracts.agent_loop import (
        AgentRole,
        LoopLimits,
        ProgressObservation,
    )
    from meshpipeline.contracts.model_inference import (
        ModelRoundResult,
        ProviderAttemptInfo,
        ToolCallRequest,
    )

    class _Ext:
        def sanitized(self): return {"kind": "cancel-fence"}

    class _Driver:
        role = AgentRole.builder

        def limits(self): return LoopLimits(max_rounds=4)
        def category_of(self, tool): return "navigation"
        def observe(self, tally): return ProgressObservation(made_progress=True, signature="s")
        def correction(self, stage, tally, observation): return None
        def before_round(self, tally, messages): return None
        def forced_tool(self, tally): return None
        def is_supersession(self, exc): return isinstance(exc, fence.StaleWorkerFenced)
        def extension(self): return _Ext()
        def on_plaintext(self, tally): return RoundDecision(complete=True)
        async def close_out(self, tally): return None

        async def execute(self, invocation):
            hang.set()
            await asyncio.Event().wait()          # never resolves; the test cancels it

    async def _provider(**kw):
        return ModelRoundResult(
            tool_calls=(ToolCallRequest(id="c1", name="t", arguments="{}"),),
            assistant_text="", finish_reason="tool_calls",
            provider=ProviderAttemptInfo(1, "p", "m"))

    return _Driver(), _provider


async def _cancel_a_live_loop(job_id: str, records: list) -> BaseException:
    from meshpipeline.agents.loop.runner import run_agent_loop

    hang = asyncio.Event()
    driver, provider = _loop_pieces(records, hang=hang)
    task = asyncio.create_task(run_agent_loop(
        driver=driver, provider_call=provider, messages=[], tools=[], job_id=job_id,
        append_tool_result=lambda m, cid, c: m.append(
            {"role": "tool", "tool_call_id": cid, "content": c}),
        deadline_s=30.0, record_sink=records.append))
    await asyncio.wait_for(hang.wait(), timeout=10)
    task.cancel()
    try:
        await task
    except BaseException as exc:
        return exc
    raise AssertionError("the cancelled loop completed normally")


# the guarantees

async def test_a_cancelled_owner_writes_no_record_and_keeps_its_ownership(SessionLocal):
    jid, own = await _claimed(SessionLocal)
    before = await _owner_row(SessionLocal, jid)

    records: list = []
    with fence.execution_ownership(own, session_factory=SessionLocal):
        exc = await _cancel_a_live_loop(str(jid), records)

    assert isinstance(exc, asyncio.CancelledError)
    assert records == [], f"a cancelled worker wrote {len(records)} authoritative record(s)"
    assert await _owner_row(SessionLocal, jid) == before, (
        "cancellation cleared, bumped or overwrote the ownership row")


async def test_cancellation_does_not_advance_the_checkpoint(SessionLocal):
    jid, own = await _claimed(SessionLocal)
    async with await _saver() as inner:
        await inner.setup()
        ck = FencedCheckpointer(inner, job_id=str(jid),
                                execution_generation=own.execution_generation,
                                approved_intent_fingerprint="fp-1",
                                state_schema_version=STATE_SCHEMA_VERSION)
        cfg = _cfg(jid, own.execution_generation)
        with fence.execution_ownership(own, session_factory=SessionLocal):
            await ck.aput(cfg, _checkpoint("after-builder-round-1"),
                          {"source": "loop", "step": 1}, {})

            records: list = []
            exc = await _cancel_a_live_loop(str(jid), records)
            assert isinstance(exc, asyncio.CancelledError)
            assert records == []

        got = await ck.aget_tuple(cfg)
        assert got.checkpoint["channel_values"]["stage"] == "after-builder-round-1", (
            "the cancelled operation advanced the durable resume point")


async def test_the_cancelled_generation_can_still_continue_and_finish(SessionLocal):
    jid, own = await _claimed(SessionLocal)

    records: list = []
    with fence.execution_ownership(own, session_factory=SessionLocal):
        await _cancel_a_live_loop(str(jid), records)
    assert records == []

    async with await _saver() as inner:
        await inner.setup()
        ck = FencedCheckpointer(inner, job_id=str(jid),
                                execution_generation=own.execution_generation,
                                approved_intent_fingerprint="fp-1",
                                state_schema_version=STATE_SCHEMA_VERSION)
        cfg = _cfg(jid, own.execution_generation)
        with fence.execution_ownership(own, session_factory=SessionLocal):
            await ck.aput(cfg, _checkpoint("finished-after-a-cancelled-attempt"),
                          {"source": "loop", "step": 2}, {})
        got = await ck.aget_tuple(cfg)
        assert got.checkpoint["channel_values"]["stage"] == "finished-after-a-cancelled-attempt", (
            "the owner could not continue after one of its attempts was cancelled")


async def test_cancellation_does_not_hand_the_job_to_anyone_else(SessionLocal):
    jid, own = await _claimed(SessionLocal)

    records: list = []
    with fence.execution_ownership(own, session_factory=SessionLocal):
        await _cancel_a_live_loop(str(jid), records)

    async with SessionLocal() as s:
        res, _ = await lease.claim_execution(
            s, jid, worker_token=uuid.uuid4(), backend="celery", backend_execution_id="exec-B")
        await s.commit()
    assert res == ClaimResult.active_lease_conflict, (
        f"a cancelled worker's live lease was stealable: {res}")


async def test_the_job_is_still_takeable_by_the_normal_expiry_path(SessionLocal):
    from sqlalchemy import text

    jid, own = await _claimed(SessionLocal)

    records: list = []
    with fence.execution_ownership(own, session_factory=SessionLocal):
        await _cancel_a_live_loop(str(jid), records)
    assert records == []

    async with SessionLocal() as s:                       # the cancelled worker stops heartbeating
        await s.execute(text("update simulation_jobs set lease_expires_at = now() - "
                             "interval '1 second' where id = :j"), {"j": str(jid)})
        await s.commit()

    async with SessionLocal() as s:
        res, succ = await lease.claim_execution(
            s, jid, worker_token=uuid.uuid4(), backend="celery", backend_execution_id="exec-B")
        await s.commit()
    assert res == ClaimResult.acquired_new_generation, (
        f"a cancelled job could not be taken over after its lease lapsed: {res}")
    assert succ.execution_generation > own.execution_generation

    async with await _saver() as inner:
        await inner.setup()
        ck = FencedCheckpointer(inner, job_id=str(jid),
                                execution_generation=succ.execution_generation,
                                approved_intent_fingerprint="fp-1",
                                state_schema_version=STATE_SCHEMA_VERSION)
        cfg = _cfg(jid, succ.execution_generation)
        with fence.execution_ownership(succ, session_factory=SessionLocal):
            await ck.aput(cfg, _checkpoint("successor-finished"),
                          {"source": "loop", "step": 1}, {})
        got = await ck.aget_tuple(cfg)
        assert got.checkpoint["channel_values"]["stage"] == "successor-finished"


async def test_a_cancelled_stale_generation_writes_nothing_durable(SessionLocal):
    jid, own = await _claimed(SessionLocal)
    async with SessionLocal() as s:                       # a newer generation takes over
        row = await s.get(SimulationJob, jid)
        row.execution_generation += 1
        row.active_worker_token = uuid.uuid4()
        await s.commit()
    after_takeover = await _owner_row(SessionLocal, jid)

    records: list = []
    with fence.execution_ownership(own, session_factory=SessionLocal):
        exc = await _cancel_a_live_loop(str(jid), records)
    assert isinstance(exc, asyncio.CancelledError)
    assert records == [], "a cancelled stale generation wrote an authoritative record"

    async with await _saver() as inner:
        await inner.setup()
        ck = FencedCheckpointer(inner, job_id=str(jid),
                                execution_generation=own.execution_generation,
                                approved_intent_fingerprint="fp-1",
                                state_schema_version=STATE_SCHEMA_VERSION)
        with fence.execution_ownership(own, session_factory=SessionLocal):
            with pytest.raises(fence.StaleWorkerFenced):
                await ck.aput(_cfg(jid, own.execution_generation), _checkpoint("stale-after-cancel"),
                              {"source": "loop", "step": 9}, {})

    assert await _owner_row(SessionLocal, jid) == after_takeover, (
        "a cancelled stale worker disturbed the current owner")


async def test_no_terminal_event_is_published_by_a_cancelled_worker(SessionLocal):
    from sqlalchemy import func as sa_func
    from sqlalchemy import select

    from meshpipeline.persistence.models import TerminalOutbox

    jid, own = await _claimed(SessionLocal)

    async def _outbox_count() -> int:
        async with SessionLocal() as s:
            got = await s.execute(select(sa_func.count()).select_from(TerminalOutbox)
                                  .where(TerminalOutbox.job_id == jid))
            return int(got.scalar_one())

    before = await _outbox_count()
    records: list = []
    with fence.execution_ownership(own, session_factory=SessionLocal):
        await _cancel_a_live_loop(str(jid), records)
    assert records == []
    assert await _outbox_count() == before, (
        "a cancelled worker published a terminal/failure event")
