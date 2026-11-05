# Responsibility: Verify a cancel evicts the worker, ends the job once, announces it, withdraws the launch, and refuses what it may not touch.
# Boundaries: the authority's own order and refusals; the fences it relies on are proven in test_cancelled_job_fence.
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from meshpipeline.application import job_cancel
from meshpipeline.persistence.job_state import TransitionResult
from meshpipeline.persistence.models import JobStatus

JOB = uuid.uuid4()
OWNER = "engineer@example.com"
ORG = str(uuid.uuid4())


class _Session:
    def __init__(self, log): self.log = log
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def commit(self): self.log.append(("commit",))


def _row(status=JobStatus.running, token="held", reason=None):
    return SimpleNamespace(
        id=JOB, owner_id=OWNER, status=status,
        active_worker_token=(uuid.uuid4() if token == "held" else token),
        execution_generation=2, current_attempt=1, cancel_reason=reason,
        dispatch_payload={"mesh_engine": "cfmesh", "purpose": "external_cfd",
                          "dimensionality": "3D", "approved_snapshot_id": "snap-1"})


class _Jobs:
    def __init__(self, row, log): self.row = row; self.log = log; self.final = None

    async def lock_for_owner(self, db, job_id, owner_id, *, organization_id=""):
        self.log.append(("lock", job_id, owner_id, organization_id))
        return self.row if (self.row is not None and owner_id == OWNER) else None

    async def transition(self, db, job_id, target, *, allow=None):
        self.log.append(("transition", target))
        if self.row.status in (JobStatus.succeeded, JobStatus.failed, JobStatus.cancelled):
            return TransitionResult.rejected_current_state
        self.row.status = target
        return TransitionResult.applied

    async def set_final_result(self, db, job_id, final_result):
        self.log.append(("final_result", final_result["status"]))
        self.final = final_result


class _Lease:
    def __init__(self, row, log): self.row = row; self.log = log

    async def evict_owner(self, db, job_id, *, now=None):
        self.log.append(("evict", job_id))
        had = self.row.active_worker_token is not None
        self.row.active_worker_token = None
        return had


class _Outbox:
    def __init__(self, log): self.log = log; self.rows = []

    async def enqueue(self, db, **kw):
        self.log.append(("outbox", kw["terminal_status"]))
        self.rows.append(kw)
        return True


@pytest.fixture
def wired(monkeypatch):
    log: list = []
    revoked: list = []
    delivered: list = []

    class _Launcher:
        async def launch(self, db, job_id, payload): raise AssertionError("a cancel never launches")
        async def revoke(self, job_id): revoked.append(job_id)

    import meshpipeline.contracts.pipeline_execution as pe
    monkeypatch.setattr(pe, "_launcher", _Launcher())

    import meshpipeline.application.outbox_publisher as obp

    async def _deliver(session_factory, job_id, *, repo=None):
        delivered.append(str(job_id))
        return obp.PublishStats(published=1)
    monkeypatch.setattr(obp, "deliver_own_terminal_event", _deliver)
    return {"log": log, "revoked": revoked, "delivered": delivered}


async def _cancel(row, wired, *, reason="", owner=OWNER):
    log = wired["log"]
    jobs, outbox = _Jobs(row, log), _Outbox(log)
    lease = _Lease(row if row is not None else SimpleNamespace(active_worker_token=None), log)
    out = await job_cancel.cancel_job(
        JOB, owner_id=owner, organization_id=ORG, reason=reason,
        session_factory=lambda: _Session(log), job_repo=jobs, lease_repo=lease, outbox_repo=outbox)
    return out, jobs, outbox


def _kinds(wired):
    return [e[0] for e in wired["log"]]


# the cancel itself

async def test_a_running_job_is_evicted_ended_announced_and_its_launch_withdrawn(wired):
    row = _row()
    out, jobs, outbox = await _cancel(row, wired, reason="wrong file")

    assert out.result is job_cancel.CancelResult.cancelled
    assert out.status is JobStatus.cancelled and out.cancel_reason == "wrong file"
    assert row.status is JobStatus.cancelled and row.cancel_reason == "wrong file"
    assert row.active_worker_token is None, "the worker still owns the job"
    # THE ORDER: lock, evict under the lock, the CAS, the record, the outbox, one commit
    assert _kinds(wired) == ["lock", "evict", "transition", "final_result", "outbox", "commit"]
    assert wired["log"][0] == ("lock", JOB, OWNER, ORG), "the read is not tenant-scoped"
    assert jobs.final["status"] == "cancelled" and jobs.final["job_id"] == str(JOB)
    assert jobs.final["engine"] == "cfmesh", "the durable intent was not carried into the record"
    (enq,) = outbox.rows
    assert enq["terminal_status"] == "cancelled" and enq["execution_generation"] == 2
    assert enq["event_payload"]["closing_message"].startswith("Cancelled by you.")
    assert enq["event_payload"]["final_result"] == jobs.final
    # AFTER the commit: the queued launch is withdrawn and the closing line delivered
    assert wired["revoked"] == [str(JOB)] and wired["delivered"] == [str(JOB)]


async def test_a_pending_job_no_worker_claimed_is_cancelled_the_same_way(wired):
    row = _row(status=JobStatus.pending, token=None)
    out, jobs, outbox = await _cancel(row, wired)
    assert out.result is job_cancel.CancelResult.cancelled and row.status is JobStatus.cancelled
    assert _kinds(wired) == ["lock", "evict", "transition", "final_result", "outbox", "commit"]
    assert wired["revoked"] == [str(JOB)], "the queued launch was left to run"


async def test_a_repeat_cancel_changes_nothing_and_answers_the_same(wired):
    row = _row(status=JobStatus.cancelled, token=None, reason="earlier")
    out, jobs, outbox = await _cancel(row, wired, reason="again")
    assert out.result is job_cancel.CancelResult.already_cancelled
    assert out.status is JobStatus.cancelled and out.cancel_reason == "earlier"
    assert row.cancel_reason == "earlier", "a repeat rewrote the first reason"
    assert _kinds(wired) == ["lock"], f"a repeat wrote something: {_kinds(wired)}"
    assert wired["revoked"] == [] and wired["delivered"] == []


@pytest.mark.parametrize("status", [JobStatus.succeeded, JobStatus.failed])
async def test_a_finished_job_is_refused_untouched(wired, status):
    row = _row(status=status, token=None)
    out, jobs, outbox = await _cancel(row, wired)
    assert out.result is job_cancel.CancelResult.already_finished and out.status is status
    assert row.status is status and _kinds(wired) == ["lock"]
    assert wired["revoked"] == [] and wired["delivered"] == []


async def test_a_foreign_or_missing_job_reads_as_not_found(wired):
    out, *_ = await _cancel(_row(), wired, owner="stranger@example.com")
    assert out.result is job_cancel.CancelResult.not_found and out.status is None
    out, *_ = await _cancel(None, wired)
    assert out.result is job_cancel.CancelResult.not_found
    assert wired["revoked"] == [] and wired["delivered"] == []


# the reason

async def test_the_reason_is_clipped_and_blank_means_none(wired):
    row = _row()
    out, *_ = await _cancel(row, wired, reason="  " + "x" * 900)
    assert row.cancel_reason == "x" * job_cancel.REASON_MAX
    assert out.cancel_reason == row.cancel_reason
    row2 = _row()
    out2, *_ = await _cancel(row2, wired, reason="   ")
    assert row2.cancel_reason is None and out2.cancel_reason is None


# dependencies that fail after the commit never undo it

async def test_a_broker_that_refuses_the_revoke_does_not_undo_the_cancel(wired, monkeypatch):
    import meshpipeline.contracts.pipeline_execution as pe

    class _Down:
        async def revoke(self, job_id): raise ConnectionError("broker down")
    monkeypatch.setattr(pe, "_launcher", _Down())
    row = _row()
    out, *_ = await _cancel(row, wired)
    assert out.result is job_cancel.CancelResult.cancelled and row.status is JobStatus.cancelled
    assert wired["delivered"] == [str(JOB)], "the closing line was not delivered after a revoke failure"


async def test_no_launcher_composed_is_not_an_error(wired, monkeypatch):
    import meshpipeline.contracts.pipeline_execution as pe
    monkeypatch.setattr(pe, "_launcher", None)
    row = _row()
    out, *_ = await _cancel(row, wired)
    assert out.result is job_cancel.CancelResult.cancelled
    assert wired["delivered"] == [str(JOB)]


async def test_a_transition_that_does_not_apply_is_a_loud_defect_not_a_silent_cancel(wired):
    row = _row()

    class _Stuck(_Jobs):
        async def transition(self, db, job_id, target, *, allow=None):
            return TransitionResult.rejected_current_state
    jobs = _Stuck(row, wired["log"])
    with pytest.raises(RuntimeError, match="did not apply"):
        await job_cancel.cancel_job(
            JOB, owner_id=OWNER, organization_id=ORG,
            session_factory=lambda: _Session(wired["log"]), job_repo=jobs,
            lease_repo=_Lease(row, wired["log"]), outbox_repo=_Outbox(wired["log"]))
    assert ("commit",) not in wired["log"] and wired["revoked"] == []


# the route is transport

def test_the_route_is_transport_over_the_authority():
    import inspect

    from meshpipeline.api.v1 import simulation

    src = inspect.getsource(simulation.cancel_job)
    assert "job_cancel.cancel_job(" in src
    for owned in ("transition(", "evict_owner(", "enqueue(", "get_db(", "revoke("):
        assert owned not in src, f"the route performs the authority's work itself: {owned}"


def test_both_launch_backends_answer_the_revoke():
    from meshpipeline.adapters.pipeline_execution import celery as celery_pipeline
    from meshpipeline.adapters.pipeline_execution import deferred as deferred_pipeline

    for backend in (celery_pipeline, deferred_pipeline):
        assert callable(getattr(backend, "revoke", None)), f"{backend.__name__} cannot revoke"


async def test_the_celery_backend_revokes_the_task_named_after_the_job(monkeypatch):
    from meshpipeline.adapters.pipeline_execution import celery as celery_pipeline

    seen: list = []

    class _Control:
        def revoke(self, task_id, **kw):
            seen.append((task_id, kw))
    # the hermetic tier stubs the Celery app without a control surface; add one rather than require it
    monkeypatch.setattr(celery_pipeline.celery_app, "control", _Control(), raising=False)
    await celery_pipeline.revoke(str(JOB))
    assert seen == [(str(JOB), {})], "the revoke names a different task, or terminates a running one"
