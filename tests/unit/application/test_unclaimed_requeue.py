# Responsibility: Verify a job whose run died BEFORE claiming it, on a database that was only
# unavailable, is put back on the queue with its own arguments (and its delivery forgiven) instead of
# being left pending with no task - and that every other crash takes the usual crash path.
# Boundaries: a stood-in launcher and delivery guard; no broker, no database.
from __future__ import annotations

import asyncio
import inspect
import uuid

import pytest

from meshpipeline.application import unclaimed_requeue as U


class TooManyConnectionsError(Exception):
    """Named like asyncpg's: 'remaining connection slots are reserved...'."""


class _Launcher:
    RUNS_WORK = True

    def __init__(self, fail=False):
        self.calls: list[tuple] = []
        self.fail = fail

    async def launch(self, db, job_id, payload):
        if self.fail:
            raise RuntimeError("broker down")
        self.calls.append((db, job_id, payload))

    async def revoke(self, job_id):
        return None


class _Deferred(_Launcher):
    RUNS_WORK = False


def _run_kwargs(job_id: str) -> dict:
    from meshpipeline.application import pipeline_run
    sig = inspect.signature(pipeline_run.run_pipeline)
    kw = {n: p.default for n, p in sig.parameters.items() if p.default is not inspect.Parameter.empty}
    return {**kw, "job_id": job_id, "owner_id": "tenant-a", "request_txt": "mesh it"}


@pytest.fixture
def env(monkeypatch):
    from meshpipeline.contracts import delivery_guard, pipeline_execution

    forgiven: list[str] = []
    monkeypatch.setattr(delivery_guard, "forgive_attempt", lambda job_id: forgiven.append(job_id))
    monkeypatch.setattr("meshpipeline.application.maintenance.cleanup.publish_job_note",
                        lambda *a, **k: None)
    launcher = _Launcher()
    previous = pipeline_execution._launcher
    pipeline_execution.set_pipeline_launcher(launcher)
    job_id = str(uuid.uuid4())
    token = U.RUN_KWARGS.set(_run_kwargs(job_id))
    slept: list[float] = []

    async def _sleep(s):
        slept.append(s)

    yield {"job_id": job_id, "launcher": launcher, "forgiven": forgiven, "slept": slept, "sleep": _sleep,
           "set": pipeline_execution.set_pipeline_launcher}
    U.RUN_KWARGS.reset(token)
    pipeline_execution._launcher = previous


def _requeue(env, exc, *, claimed=False):
    return asyncio.run(U.requeue_if_unclaimed(env["job_id"], exc, claimed=claimed, sleep=env["sleep"]))


def test_an_unclaimed_job_on_a_full_database_goes_back_on_the_queue(env):
    out = _requeue(env, TooManyConnectionsError("remaining connection slots are reserved"))
    assert out == {"job_id": env["job_id"], "status": "requeued", "reason": "unclaimed_dependency_unavailable"}
    ((db, job_id, payload),) = env["launcher"].calls
    assert job_id == env["job_id"] and payload["job_id"] == env["job_id"]
    assert payload["schema_version"] and payload["request_txt"] == "mesh it"   # the API's own launch form
    assert env["slept"] == [U.REQUEUE_AFTER_S]
    assert env["forgiven"] == [env["job_id"]]                 # not counted toward the poison cap


def test_the_message_form_of_a_full_database_counts_too(env):
    assert _requeue(env, RuntimeError("FATAL: sorry, too many clients already")) is not None


@pytest.mark.parametrize("why,exc,claimed", [
    ("claimed: the lease reaper owns it", TooManyConnectionsError("full"), True),
    ("a defect, not an outage", ValueError("bad"), False),
])
def test_anything_else_takes_the_usual_crash_path(env, why, exc, claimed):
    assert _requeue(env, exc, claimed=claimed) is None, why
    assert not env["launcher"].calls and not env["forgiven"]


def test_no_run_arguments_or_another_jobs_means_no_requeue(env):
    U.RUN_KWARGS.set(None)
    assert _requeue(env, TooManyConnectionsError("full")) is None
    U.RUN_KWARGS.set(_run_kwargs(str(uuid.uuid4())))
    assert _requeue(env, TooManyConnectionsError("full")) is None
    assert not env["launcher"].calls


def test_a_launcher_that_runs_nothing_by_itself_or_fails_falls_back(env):
    env["set"](_Deferred())
    assert _requeue(env, TooManyConnectionsError("full")) is None
    env["set"](_Launcher(fail=True))
    assert _requeue(env, TooManyConnectionsError("full")) is None
    assert not env["forgiven"]


def test_run_pipeline_hands_its_own_arguments_to_the_requeue(monkeypatch):
    from meshpipeline.application import pipeline_run

    seen = {}

    async def _fake_run_async(req):
        seen.update(U.RUN_KWARGS.get() or {})
        return {"job_id": req.job_id, "status": "requeued", "verdict": ""}

    monkeypatch.setattr(pipeline_run, "_run_async", _fake_run_async)
    job_id = str(uuid.uuid4())
    pipeline_run.run_pipeline(job_id=job_id, owner_id="o", request_txt="hello", purpose="structural")
    assert seen["job_id"] == job_id and seen["request_txt"] == "hello" and seen["purpose"] == "structural"
    from meshpipeline.application.dispatch_contract import accepted_keys
    assert set(seen) == set(accepted_keys())                  # exactly the run entry's arguments
