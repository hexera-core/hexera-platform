# Responsibility: Verify a restart resumes at each interruption point, and the abandoned worker is fenced there.
from __future__ import annotations

import uuid
from typing import Annotated, TypedDict

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests import harness_provisioning as hp

import meshpipeline.settings.providers as provcfg
from meshpipeline.application import execution_fence as fence
from meshpipeline.application.fenced_checkpointer import FencedCheckpointer
from meshpipeline.contracts.pipeline_state import STATE_SCHEMA_VERSION
from meshpipeline.persistence.lease import ClaimResult, LeaseRepository
from meshpipeline.persistence.models import JobStatus, SimulationJob

lease = LeaseRepository()

# The eight restart points, in pipeline order.
POINTS = [
    "after_admission",
    "after_planner",
    "after_builder",
    "after_native_before_accept",
    "during_reviewer",
    "after_reviewer_pass",
    "after_artifact_ready",
    "after_terminal_before_publish",
]


def _append(a: list, b: list) -> list:
    return (a or []) + (b or [])


class RunState(TypedDict, total=False):
    job_id: str
    approved_intent_fingerprint: str
    pipeline_deadline_epoch: float
    execution_generation: int
    ran: Annotated[list, _append]        # every node that EXECUTED, in order


def _node(name: str):
    async def _fn(state: RunState) -> dict:
        # the same fence the production graph wrapper applies to every node
        await fence.assert_current_owner(f"graph node {name}")
        return {"ran": [name]}
    return _fn


def _build(checkpointer, *, interrupt_before=None):
    b = StateGraph(RunState)
    for n in POINTS:
        b.add_node(n, _node(n))
    b.add_edge(START, POINTS[0])
    for a, c in zip(POINTS, POINTS[1:]):
        b.add_edge(a, c)
    b.add_edge(POINTS[-1], END)
    return b.compile(checkpointer=checkpointer, interrupt_before=interrupt_before or [])


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


def _pg_dsn() -> str:
    return provcfg.POSTGRES_DSN.replace("+asyncpg", "").replace("+psycopg", "")


async def _claim(SessionLocal, jid, *, exec_id, token=None, now=None):
    async with SessionLocal() as s:
        res, own = await lease.claim_execution(
            s, jid, worker_token=token or uuid.uuid4(), backend="celery",
            backend_execution_id=exec_id, now=now)
        await s.commit()
    return res, own


def _lapsed():
    import datetime as _dt
    return _dt.datetime.now(_dt.UTC) - _dt.timedelta(hours=2)


async def _new_job(SessionLocal) -> uuid.UUID:
    async with SessionLocal() as s:
        j = SimulationJob(owner_id="o", status=JobStatus.pending)
        s.add(j)
        await s.commit()
        return j.id


def _cfg(jid, generation):
    return {"configurable": {"thread_id": f"{jid}:s{STATE_SCHEMA_VERSION}:g{generation}"}}


def _wrap(inner, jid, own, fp="fp-approved"):
    return FencedCheckpointer(inner, job_id=str(jid),
                              execution_generation=own.execution_generation,
                              approved_intent_fingerprint=fp,
                              state_schema_version=STATE_SCHEMA_VERSION)


# the eight restart points
@pytest.mark.parametrize("point", POINTS)
async def test_same_execution_restart_resumes_at_each_point(SessionLocal, point):
    jid = await _new_job(SessionLocal)
    res, own = await _claim(SessionLocal, jid, exec_id="exec-A", now=_lapsed())
    assert res == ClaimResult.acquired_new_generation
    deadline_before = own.pipeline_deadline_at
    cfg = _cfg(jid, own.execution_generation)
    seed = {"job_id": str(jid), "approved_intent_fingerprint": "fp-approved",
            "pipeline_deadline_epoch": deadline_before.timestamp(),
            "execution_generation": own.execution_generation, "ran": []}

 # worker 1: run until the restart point, then abandon it
    async with AsyncPostgresSaver.from_conn_string(_pg_dsn()) as saver1:
        await saver1.setup()
        g1 = _build(_wrap(saver1, jid, own), interrupt_before=[point])
        with fence.execution_ownership(own, session_factory=SessionLocal):
            try:
                await g1.ainvoke(seed, config=cfg)     # stops BEFORE `point`, checkpointing progress
            except GraphInterrupt:
                pass                                   # the interrupt IS the stop signal
    before = POINTS[:POINTS.index(point)]

 # worker 2: SAME logical execution restarts (same durable backend execution id)
    res2, own2 = await _claim(SessionLocal, jid, exec_id="exec-A")
    assert res2 == ClaimResult.resumed_same_generation
    assert own2.execution_generation == own.execution_generation      # generation KEPT
    assert own2.worker_token != own.worker_token                      # token ROTATED
    assert own2.pipeline_deadline_at == deadline_before               # deadline NOT reset

    async with AsyncPostgresSaver.from_conn_string(_pg_dsn()) as saver2:   # NEW saver + connection
        await saver2.setup()
        g2 = _build(_wrap(saver2, jid, own2))
        with fence.execution_ownership(own2, session_factory=SessionLocal):
            final = await g2.ainvoke(None, config=cfg)                # resume from the checkpoint

    # every node ran EXACTLY once across the two workers - nothing was redone, nothing skipped
    assert final["ran"] == POINTS
    assert final["ran"][:len(before)] == before
    # the approved-intent binding and the deadline survived the restart untouched
    assert final["approved_intent_fingerprint"] == "fp-approved"
    assert final["pipeline_deadline_epoch"] == deadline_before.timestamp()
    # and the OLD token is fenced afterwards
    async with SessionLocal() as s:
        assert await lease.heartbeat(s, own) is False


@pytest.mark.parametrize("point", POINTS)
async def test_the_abandoned_worker_is_fenced_at_each_point(SessionLocal, point):
    jid = await _new_job(SessionLocal)
    _, own1 = await _claim(SessionLocal, jid, exec_id="exec-A", now=_lapsed())
    cfg = _cfg(jid, own1.execution_generation)
    seed = {"job_id": str(jid), "approved_intent_fingerprint": "fp-approved",
            "pipeline_deadline_epoch": own1.pipeline_deadline_at.timestamp(),
            "execution_generation": own1.execution_generation, "ran": []}

    async with AsyncPostgresSaver.from_conn_string(_pg_dsn()) as saver1:
        await saver1.setup()
        g1 = _build(_wrap(saver1, jid, own1), interrupt_before=[point])
        with fence.execution_ownership(own1, session_factory=SessionLocal):
            try:
                await g1.ainvoke(seed, config=cfg)
            except GraphInterrupt:
                pass

    # snapshot the durable progress at the moment worker 1 is abandoned
    async with AsyncPostgresSaver.from_conn_string(_pg_dsn()) as reader:
        snap = await reader.aget_tuple(cfg)
        progress_before = list(snap.checkpoint["channel_values"].get("ran") or []) if snap else []

    # worker 2 takes the token (a same-execution restart)
    _, own2 = await _claim(SessionLocal, jid, exec_id="exec-A")
    assert own2.worker_token != own1.worker_token

    # worker 1 tries to keep checkpointing: REFUSED
    async with AsyncPostgresSaver.from_conn_string(_pg_dsn()) as saver_stale:
        stale_ck = _wrap(saver_stale, jid, own1)
        with fence.execution_ownership(own1, session_factory=SessionLocal):
            with pytest.raises(fence.StaleWorkerFenced):
                await stale_ck.aput(
                    cfg, {"v": 1, "id": "stale", "ts": "2026-07-24T00:00:00+00:00",
                          "channel_values": {"ran": progress_before + ["STALE"]},
                          "channel_versions": {}, "versions_seen": {}},
                    {"source": "loop", "step": 99}, {})
            with pytest.raises(fence.StaleWorkerFenced):
                await stale_ck.aput_writes(cfg, [("ran", ["STALE"])], "task-stale")

    # the durable checkpoint did NOT move
    async with AsyncPostgresSaver.from_conn_string(_pg_dsn()) as reader2:
        after = await reader2.aget_tuple(cfg)
        progress_after = list(after.checkpoint["channel_values"].get("ran") or []) if after else []
    assert progress_after == progress_before
    assert "STALE" not in progress_after


# a DIFFERENT execution takes over: new generation, fresh thread, documented re-run
async def test_different_execution_takeover_starts_a_fresh_generation(SessionLocal):
    jid = await _new_job(SessionLocal)
    res, own1 = await _claim(SessionLocal, jid, exec_id="exec-A", now=_lapsed())
    assert res == ClaimResult.acquired_new_generation
    cfg1 = _cfg(jid, own1.execution_generation)
    seed = {"job_id": str(jid), "approved_intent_fingerprint": "fp-approved",
            "pipeline_deadline_epoch": own1.pipeline_deadline_at.timestamp(),
            "execution_generation": own1.execution_generation, "ran": []}

    async with AsyncPostgresSaver.from_conn_string(_pg_dsn()) as saver1:
        await saver1.setup()
        g1 = _build(_wrap(saver1, jid, own1), interrupt_before=["after_builder"])
        with fence.execution_ownership(own1, session_factory=SessionLocal):
            try:
                await g1.ainvoke(seed, config=cfg1)
            except GraphInterrupt:
                pass

    # a DIFFERENT execution takes over the expired lease
    res2, own2 = await _claim(SessionLocal, jid, exec_id="exec-B")
    assert res2 == ClaimResult.acquired_new_generation
    assert own2.execution_generation == own1.execution_generation + 1
    assert own2.pipeline_deadline_at == own1.pipeline_deadline_at      # deadline still NOT reset

    cfg2 = _cfg(jid, own2.execution_generation)
    async with AsyncPostgresSaver.from_conn_string(_pg_dsn()) as saver2:
        await saver2.setup()
        assert await saver2.aget_tuple(cfg2) is None                   # a FRESH thread
        g2 = _build(_wrap(saver2, jid, own2))
        with fence.execution_ownership(own2, session_factory=SessionLocal):
            final = await g2.ainvoke({**seed, "execution_generation": own2.execution_generation,
                                      "ran": []}, config=cfg2)
    assert final["ran"] == POINTS          # documented: a new generation re-runs from the start
    # the previous generation's checkpoint is untouched and still its own
    async with AsyncPostgresSaver.from_conn_string(_pg_dsn()) as saver3:
        old = await saver3.aget_tuple(cfg1)
        assert old is not None
        assert old.metadata["meshpipeline"]["execution_generation"] == own1.execution_generation


# restart after the terminal transaction, before publication
async def test_restart_after_terminal_leaves_the_outbox_recoverable(SessionLocal):
    from meshpipeline.application import terminal_finalize as tfin
    from meshpipeline.application.final_result import (
        TerminalStatus,
        build_final_result,
        render_message,
    )
    from meshpipeline.persistence.repositories.terminal_outbox_repository import (
        TerminalOutboxRepository,
    )
    outbox = TerminalOutboxRepository()

    jid = await _new_job(SessionLocal)
    _, own = await _claim(SessionLocal, jid, exec_id="exec-A", now=_lapsed())
    fr = build_final_result(
        job_id=str(jid), owner_id="o", status=TerminalStatus.succeeded, engine="cfmesh",
        purpose="internal_cfd", dimensionality="3d", approved_snapshot_id="s",
        executor_success=True, reviewer_verdict="PASS", failed_gate="", api_failure="",
        attempts=1, attempts_max=3, required_ready=True, delivered_types=["mesh_bundle"],
        optional_warnings=[])
    async with SessionLocal() as db:
        out = await tfin.finalize_terminal_atomic(
            db, ownership=own, intended_status=JobStatus.succeeded, failed_reason=None,
            final_result_dict=fr.to_dict(), closing_message=render_message(fr))
        await db.commit()
    assert out.fenced is False and out.enqueued is True

    # the worker dies here. A restart must NOT be able to re-finalize: the job is already terminal,
    # so the claim itself is refused.
    res2, own2 = await _claim(SessionLocal, jid, exec_id="exec-A", now=_lapsed())
    assert res2 == ClaimResult.already_terminal and own2 is None
    async with SessionLocal() as s:
        row = await s.get(SimulationJob, jid)
        assert row.status == JobStatus.succeeded      # exactly one terminal status
        rows = await outbox.get_by_job(s, jid)
        assert len(rows) == 1 and rows[0].published_at is None   # still recoverable


async def test_a_terminal_job_cannot_be_reclaimed_after_restart(SessionLocal):
    jid = await _new_job(SessionLocal)
    _, own = await _claim(SessionLocal, jid, exec_id="exec-A")
    async with SessionLocal() as s:
        row = await s.get(SimulationJob, jid)
        row.status = JobStatus.succeeded
        await s.commit()
    res, own2 = await _claim(SessionLocal, jid, exec_id="exec-A")
    assert res == ClaimResult.already_terminal and own2 is None
