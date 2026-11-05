# Responsibility: Verify a real claimed execution reaches graph.ainvoke under the exact durable ownership.
# Boundaries: the graph-invocation boundary only - engine, native and in-graph node paths are other suites.
from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import meshpipeline.settings.providers as provcfg
from meshpipeline.application import execution_fence as fence
from meshpipeline.persistence.models import SimulationJob

pytestmark = pytest.mark.asyncio

if not os.getenv("DATABASE_URL"):
    pytest.skip("a real PostgreSQL endpoint is required", allow_module_level=True)


class _ControlledGraphFailure(RuntimeError):
    pass


class _AbsentSnapshot:
    created_at = None
    next: tuple = ()
    values: dict = {}


# The probe stands in for the compiled graph and does nothing a graph may not do: it READS the
# context it was invoked in. It never claims, binds or fabricates ownership, never patches
# current_ownership, the lease repository or any PostgreSQL check, and adds no production hook -
# `build_graph` is the composition seam _run_async already imports.
class _ProbeGraph:
    def __init__(self, seen: dict, entered: asyncio.Event, release: asyncio.Event, *, mode: str):
        self._seen, self._entered, self._release, self._mode = seen, entered, release, mode

    # `_classify_checkpoint` builds a graph purely to read the thread before anything is claimed.
    # A snapshot with no `created_at` is exactly what an absent thread looks like, so this run
    # classifies as fresh - the disposition every other case here already assumes.
    async def aget_state(self, config=None):
        return _AbsentSnapshot()

    async def ainvoke(self, state, config=None):
        self._seen["at_entry"] = fence.current_ownership()
        self._seen["entry_task"] = asyncio.current_task().get_name()
        self._entered.set()
        await self._release.wait()          # a barrier, never a sleep or a timeout

        async def _nested():
            return fence.current_ownership()

        self._seen["nested"] = await _nested()
        self._seen["thread"] = await asyncio.to_thread(fence.current_ownership)
        if self._mode == "raise":
            raise _ControlledGraphFailure("controlled graph failure")
        return {"job_id": self._seen.get("job_id"), "status": "probe-complete"}


# A second engine, so the durable row is read outside the run's own session and pool.
def _independent_sessions():
    engine = create_async_engine(provcfg.POSTGRES_DSN, pool_size=1, max_overflow=1,
                                 pool_pre_ping=True)
    return engine, async_sessionmaker(bind=engine, expire_on_commit=False)


async def _seed_job(job_id: uuid.UUID, owner_id: str) -> None:
    engine, Session = _independent_sessions()
    try:
        async with Session() as db:
            db.add(SimulationJob(id=job_id, owner_id=owner_id))
            await db.commit()
    finally:
        await engine.dispose()


async def _read_row(job_id: uuid.UUID):
    engine, Session = _independent_sessions()
    try:
        async with Session() as db:
            return (await db.execute(
                select(SimulationJob).where(SimulationJob.id == job_id))).scalar_one()
    finally:
        await engine.dispose()


# Run the production entry point with the probe installed, pausing inside ainvoke.
async def _drive(monkeypatch, *, mode: str):
    job_id = uuid.uuid4()
    owner_id = f"claim-root-{job_id.hex[:8]}"
    await _seed_job(job_id, owner_id)

    seen: dict = {"job_id": str(job_id)}
    entered, release = asyncio.Event(), asyncio.Event()

    import meshpipeline.pipeline.graph as graph_mod
    monkeypatch.setattr(graph_mod, "build_graph",
                        lambda checkpointer: _ProbeGraph(seen, entered, release, mode=mode))

    # The FIRST post-graph production seam the run reaches. Wrapped passively: it records the
    # context and calls the real implementation, so nothing about the fence changes.
    from meshpipeline.persistence.lease import LeaseRepository
    real_is_current_owner = LeaseRepository.is_current_owner

    async def _recording_is_current_owner(self, db, own):
        seen.setdefault("post_graph", fence.current_ownership())
        return await real_is_current_owner(self, db, own)

    monkeypatch.setattr(LeaseRepository, "is_current_owner", _recording_is_current_owner)

    # A raising graph never reaches the pre-finalize ownership check, so the crash route needs its
    # own observation point. `finalize_crash` is the first production seam it does reach. Both
    # wrappers only read the context and delegate; whichever fires first records.
    from meshpipeline.application import terminal_finalize as _tf
    real_finalize_crash = _tf.finalize_crash

    async def _recording_finalize_crash(*a, **k):
        seen.setdefault("post_graph", fence.current_ownership())
        return await real_finalize_crash(*a, **k)

    monkeypatch.setattr(_tf, "finalize_crash", _recording_finalize_crash)

    from meshpipeline.application.pipeline_run import JobRequest, _run_async
    run = asyncio.create_task(_run_async(JobRequest(job_id=str(job_id), owner_id=owner_id)))
    try:
        waiter = asyncio.create_task(entered.wait())
        done, _ = await asyncio.wait({waiter, run}, return_when=asyncio.FIRST_COMPLETED)
        if run in done:                     # the run ended before the graph - surface why
            waiter.cancel()
            raise AssertionError(f"the run never reached graph.ainvoke: {run.result()!r}")
        waiter.cancel()
        yield job_id, seen, release, run
    finally:
        release.set()
        if not run.done():
            await asyncio.wait({run})
        run.exception() if run.done() and not run.cancelled() else None


@pytest.fixture()
async def paused_success(monkeypatch):
    async for item in _drive(monkeypatch, mode="succeed"):
        yield item


@pytest.fixture()
async def paused_failure(monkeypatch):
    async for item in _drive(monkeypatch, mode="raise"):
        yield item


# the contract


async def test_the_test_holds_no_ownership_before_the_production_root_runs():
    assert fence.current_ownership() is None, \
        "the test process already had ownership bound; nothing it observes would prove anything"


async def test_the_graph_begins_under_the_exact_durably_claimed_ownership(paused_success):
    job_id, seen, release, run = paused_success

    own = seen["at_entry"]
    assert own is not None, "graph.ainvoke ran with NO ownership bound"

    # Read the claim from an independent session while the graph is held at the barrier.
    row = await _read_row(job_id)
    assert row.execution_generation >= 1, "PostgreSQL holds no claimed generation"
    assert row.active_worker_token is not None, "PostgreSQL holds no worker token"

    assert str(own.job_id) == str(job_id)
    assert own.execution_generation == row.execution_generation
    assert own.claim_epoch == row.execution_claim_epoch
    # Compared in memory only. The token itself is never printed, asserted by value, or written
    # to evidence - only whether it matches.
    assert (own.worker_token == row.active_worker_token) is True, \
        "the bound token does not match the durable row"

    release.set()
    await run


async def test_nested_await_and_to_thread_see_the_same_ownership(paused_success):
    _job_id, seen, release, run = paused_success
    release.set()
    await run

    entry, nested, thread = seen["at_entry"], seen["nested"], seen["thread"]
    assert nested is not None and thread is not None
    for name, got in (("nested await", nested), ("to_thread", thread)):
        assert got.job_id == entry.job_id, f"{name} saw a different job"
        assert got.execution_generation == entry.execution_generation, f"{name} saw another generation"
        assert got.claim_epoch == entry.claim_epoch, f"{name} saw another claim epoch"
        assert (got.worker_token == entry.worker_token) is True, f"{name} saw another token"
    assert thread is entry, "the worker thread received a copy, not the bound ownership"


async def test_the_run_is_still_inside_the_graph_while_the_barrier_holds(paused_success):
    _job_id, seen, release, run = paused_success
    assert not run.done(), "the run completed without waiting for the barrier"
    assert "nested" not in seen, "work past the barrier ran before it was released"
    release.set()
    await run
    assert "nested" in seen, "the graph never resumed after the barrier"


async def test_the_heartbeat_task_is_live_while_the_graph_is_blocked(paused_success):
    _job_id, _seen, release, run = paused_success
    beats = [t for t in asyncio.all_tasks()
             if t is not run and not t.done() and "_beat" in (t.get_coro().__qualname__ or "")]
    assert beats, "no heartbeat task was running while the graph was blocked"
    release.set()
    await run
    assert all(t.done() for t in beats), "the heartbeat outlived the run"


async def test_the_binding_is_cleared_once_the_graph_returns(paused_success):
    _job_id, seen, release, run = paused_success
    release.set()
    await run
    assert seen.get("post_graph", "absent") is None, (
        "ownership was still bound at the first post-graph seam; the binding must not leak past "
        f"graph.ainvoke (saw {type(seen.get('post_graph')).__name__})")
    assert fence.current_ownership() is None


# the exception path


async def test_a_controlled_graph_failure_clears_the_binding_and_tears_down(paused_failure):
    _job_id, seen, release, run = paused_failure
    assert seen["at_entry"] is not None, "the failing run did not begin under claimed ownership"
    release.set()
    # The existing failure path records the terminal outcome and re-raises; the test does not
    # weaken that, it just lets it happen.
    with pytest.raises(_ControlledGraphFailure):
        await run

    assert seen.get("post_graph", "absent") is None, \
        "ownership survived a raising graph; the binding must be cleared on every exit"
    assert fence.current_ownership() is None
    leftover = [t for t in asyncio.all_tasks()
                if not t.done() and "_beat" in (t.get_coro().__qualname__ or "")]
    assert leftover == [], "a heartbeat task outlived the failed run"


async def test_the_controlled_failure_is_the_one_the_probe_raised(paused_failure):
    _job_id, seen, release, run = paused_failure
    release.set()
    with pytest.raises(_ControlledGraphFailure, match="controlled graph failure"):
        await run
    assert seen["at_entry"] is not None, "the failing run did not begin under claimed ownership"
    assert seen["nested"] is not None and seen["thread"] is not None, \
        "the failing run never reached the observation points before raising"
