# Responsibility: Verify a worker that is shut down on purpose stops its run and hands the job back, instead of the job
#                 being failed as "worker lost" half an hour later.
# Boundaries: the drain watch, the hand-back sequence and the orchestrator's catch, with the lease, the job
#             repository and the launcher stubbed; the lease's SQL is tests/unit/persistence/test_lease_hand_back.py.
from __future__ import annotations

import ast
import asyncio
import uuid
from pathlib import Path

import pytest

from meshpipeline.application import worker_handoff
from meshpipeline.application.worker_handoff import (
    HANDOFF_NOTE,
    WorkerDraining,
    hand_back,
    run_unless_draining,
)
from meshpipeline.contracts import delivery_guard, pipeline_execution, worker_drain
from meshpipeline.persistence.lease import ExecutionOwnership

REPO = Path(__file__).resolve().parents[3]

# THE DEFECT THIS PINS. Shared dev, 2026-09-29: the autoscaler scaled the fleet in at 05:41, 06:39
# and 07:32 UTC, each time deleting VMs that were running mesh jobs. Nothing told the jobs: the VMs
# went down in under a minute and jobs 4c536ce5 (bend_elbow_010), 32423bfa (wing_001) and e5289e02
# (ahmed_variant_001) were each failed about thirty minutes later as worker_lost.


@pytest.fixture(autouse=True)
def _no_probe(monkeypatch):
    monkeypatch.setattr(worker_drain, "_probe", None)


def _draining_after(seconds: float):
    started = asyncio.get_running_loop().time()
    return lambda: asyncio.get_running_loop().time() - started >= seconds


# the drain watch around the graph

async def test_a_run_that_is_not_disturbed_returns_its_own_result():
    async def work():
        await asyncio.sleep(0.01)
        return {"done": True}
    assert await run_unless_draining(work(), poll_seconds=0.005) == {"done": True}


async def test_a_drain_stops_the_run_and_says_so():
    stopped = asyncio.Event()

    async def long_mesh():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            stopped.set()
            raise

    worker_drain.set_drain_probe(_draining_after(0.02))
    with pytest.raises(WorkerDraining):
        await run_unless_draining(long_mesh(), poll_seconds=0.005, cancel_grace_seconds=1)
    assert stopped.is_set(), "the graph was left running on a machine that is going away"


async def test_a_run_that_finishes_first_is_delivered_not_handed_back():
    # A finished mesh must never be thrown away to be built again elsewhere.
    async def quick():
        return "meshed"
    worker_drain.set_drain_probe(lambda: True)
    assert await run_unless_draining(quick(), poll_seconds=0.005) == "meshed"


async def test_a_step_that_swallows_the_cancel_is_handed_back_not_finalized():
    # Returning after the cancel would hand the finalizer half a run, and a verdict derived from
    # it would be false. Running the job again elsewhere is the honest outcome.
    async def swallows():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            return {"reviewer_verdict": ""}          # a half-run state
    worker_drain.set_drain_probe(_draining_after(0.02))
    with pytest.raises(WorkerDraining):
        await run_unless_draining(swallows(), poll_seconds=0.005, cancel_grace_seconds=1)


async def test_the_watch_never_starves_the_run_it_watches(monkeypatch):
    # Suites that skip provider backoff replace asyncio.sleep with a coroutine that returns at
    # once. A watch polling through it never yields, and the graph beside it never runs again -
    # which hung the unit tier's terminal-matrix tests on the first CI run of this change.
    async def _instant(*_a, **_k):
        return None
    monkeypatch.setattr(asyncio, "sleep", _instant)

    async def waits_on_io():
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        loop.call_later(0.02, fut.set_result, "meshed")
        return await fut
    assert await asyncio.wait_for(run_unless_draining(waits_on_io(), poll_seconds=0.001),
                                  timeout=5) == "meshed"


async def test_a_run_that_fails_on_its_own_keeps_its_own_failure():
    async def broken():
        raise ValueError("the builder broke")
    with pytest.raises(ValueError, match="builder broke"):
        await run_unless_draining(broken(), poll_seconds=0.005)


async def test_a_run_that_will_not_unwind_is_handed_back_anyway():
    # The hand-back fences the old execution, so a graph step that ignores its cancellation for
    # longer than the grace can write nothing afterwards; waiting on it would spend the VM's
    # shutdown budget on the one thing that cannot help.
    cancels = {"n": 0}

    async def stubborn():
        while cancels["n"] < 2:          # ignores the first cancellation, honours the second
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancels["n"] += 1
    worker_drain.set_drain_probe(lambda: True)
    task = asyncio.ensure_future(stubborn())
    try:
        with pytest.raises(WorkerDraining):
            await run_unless_draining(task, poll_seconds=0.005, cancel_grace_seconds=0.05)
        assert not task.done(), "the precondition failed: the step unwound within the grace"
    finally:
        task.cancel()
        await asyncio.wait({task}, timeout=1)


def test_a_probe_that_cannot_answer_reads_as_staying_up():
    def broken():
        raise OSError("tmp is gone")
    worker_drain.set_drain_probe(broken)
    assert worker_drain.drain_requested() is False
    worker_drain.set_drain_probe(None)
    assert worker_drain.drain_requested() is False, "a process with no probe is never draining"


# the hand-back

class _Session:
    def __init__(self):
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        self.commits += 1


class _Leases:
    def __init__(self, *, released=True):
        self.released = released
        self.calls: list[str] = []
        self.released_at = None
        self.confirmed_at = None

    async def hand_back(self, db, own, *, now=None):
        self.calls.append("hand_back")
        self.released_at = now
        return self.released

    async def confirm_requeued(self, db, job_id, released_at):
        self.calls.append("confirm_requeued")
        self.confirmed_at = released_at
        return True


class _Jobs:
    PAYLOAD = {"schema_version": 1, "job_id": "x"}

    async def get_dispatch_payload(self, db, job_id):
        return dict(self.PAYLOAD)


class _Launcher:
    RUNS_WORK = True

    def __init__(self, *, fails=False):
        self.fails = fails
        self.launched: list[tuple[str, dict]] = []

    async def launch(self, db, job_id, payload):
        if self.fails:
            raise ConnectionError("broker unreachable")
        self.launched.append((job_id, payload))

    async def revoke(self, job_id):
        pass


class _Guard:
    def __init__(self):
        self.forgiven: list[str] = []

    def record_attempt(self, job_id):
        return 1

    def forgive_attempt(self, job_id):
        self.forgiven.append(job_id)


@pytest.fixture
def world(monkeypatch):
    from meshpipeline.application.maintenance import cleanup

    said: list[tuple[str, str, str]] = []
    monkeypatch.setattr(cleanup, "publish_job_note",
                        lambda job_id, text, op_id: said.append((job_id, text, op_id)))
    guard = _Guard()
    # both are process-wide bindings; monkeypatch puts back whatever another test left there
    monkeypatch.setattr(delivery_guard, "_guard", guard)
    monkeypatch.setattr(pipeline_execution, "_launcher", None)
    own = ExecutionOwnership(job_id=uuid.uuid4(), execution_generation=2,
                             worker_token=uuid.uuid4(), backend="celery",
                             pipeline_deadline_at=None)
    return own, said, guard


async def test_the_job_is_released_and_put_back_on_the_queue(world):
    own, said, guard = world
    leases, launcher = _Leases(), _Launcher()
    pipeline_execution.set_pipeline_launcher(launcher)

    out = await hand_back(lambda: _Session(), own, lease_repo=leases, job_repo=_Jobs())

    assert out == {"job_id": str(own.job_id), "status": "handed_back"}
    # released, re-launched, and only THEN confirmed queued - the confirmation names the very
    # release it confirms, so a claim or a cancel that landed in between is never undone
    assert leases.calls == ["hand_back", "confirm_requeued"]
    assert leases.released_at is not None and leases.confirmed_at == leases.released_at
    # re-launched exactly as the API launched it: the approved dispatch payload, the same job id
    assert launcher.launched == [(str(own.job_id), _Jobs.PAYLOAD)]
    # a planned move is not a crash: it must not count toward the poison-job redelivery cap
    assert guard.forgiven == [str(own.job_id)]
    # ...and the user is told, in plain words, that it moved and needs nothing from them
    assert said == [(str(own.job_id), HANDOFF_NOTE, "handoff:g2")]
    assert "run it again" not in HANDOFF_NOTE


async def test_a_job_cancelled_meanwhile_is_not_put_back_and_nothing_is_promised(world):
    own, said, guard = world
    leases, launcher = _Leases(released=False), _Launcher()
    pipeline_execution.set_pipeline_launcher(launcher)

    out = await hand_back(lambda: _Session(), own, lease_repo=leases, job_repo=_Jobs())

    assert out["status"] == "fenced"
    assert launcher.launched == [], "a cancelled or taken-over job was put back on the queue"
    assert guard.forgiven == []
    assert said == [], "the user was promised a move that never happened"


async def test_a_job_that_cannot_be_requeued_keeps_its_release_mark_for_the_reaper(world):
    # Released but with no message on the queue: the unconfirmed release is exactly what the
    # reaper looks for, so it is left marked - and the user is not yet told it moved.
    own, said, _guard = world
    leases = _Leases()
    pipeline_execution.set_pipeline_launcher(_Launcher(fails=True))

    out = await hand_back(lambda: _Session(), own, lease_repo=leases, job_repo=_Jobs())

    assert out["status"] == "handback_unqueued"
    assert leases.calls == ["hand_back"], "an unqueued release was confirmed as queued"
    assert said == []


async def test_relaunch_refuses_a_job_with_nothing_to_launch():
    assert await worker_handoff.relaunch(None, "job-1", None) is False


# the orchestrator's catch

def _handlers_of_the_run() -> list[str]:
    tree = ast.parse((REPO / "src/meshpipeline/application/pipeline_run.py").read_text("utf-8"))
    run = next(n for n in ast.walk(tree)
               if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_async")
    outer = next(n for n in run.body if isinstance(n, ast.Try))
    return [ast.unparse(h.type) if h.type is not None else "" for h in outer.handlers]


def test_a_drain_is_caught_before_the_crash_handler_ever_sees_it():
    # The crash handler records a FAILED terminal result. Reached by a drain, it would fail the
    # job for its machine going away - the exact outcome the hand-back exists to prevent.
    handlers = _handlers_of_the_run()
    assert "WorkerDraining" in handlers, handlers
    assert handlers.index("WorkerDraining") < handlers.index("Exception"), handlers


def test_the_graph_run_is_watched_for_a_drain():
    src = (REPO / "src/meshpipeline/application/pipeline_run.py").read_text("utf-8")
    assert "run_unless_draining(" in src and "graph.ainvoke(" in src
