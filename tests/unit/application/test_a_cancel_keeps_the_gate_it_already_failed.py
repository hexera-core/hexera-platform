"""A cancel threw away the gate the run had already failed, and that gate is durable.

WHY THIS EXISTS. Job 6ddb58aa-2224-4210-a2ac-926bc2e7ac42 built a 283,410,955-cell mesh, had it
refused by `quality_floor` on four measured problems, and was then cancelled by its owner. Read
back on 2026-11-14:

    simulation_jobs.final_result ->> 'failed_gate'     ''
    simulation_jobs.final_result ->> 'failure_cause'   ''

while the run's own durable PostgreSQL checkpoint, thread 6ddb58aa-...:s15:g1, holds in each of
its last three rows:

    channel_values.executor_failed_gate      quality_floor
    channel_values.executor_failure_cause    mesh_quality

So the fact was written down, survived, and was dropped on the way into the one record anybody
reads afterwards. Two things drop it and both are needed: `job_cancel.cancel_job` calls
`merge_durable_facts` with no `checkpoint_state`, which is the argument that carries the gate, and
`build_cancelled_result` has no parameter for it and hardcodes "".

THIS IS NOT A FAILURE REPORT. A cancel stays a cancel: `failure_category` is None, `outcome_code`
is "cancelled", and the closing line is still "Cancelled by you." - `render_message` returns at
the cancelled branch and never reads `failed_gate`. What changes is only that the record stops
lying about what the run knew when the owner stopped it.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from meshpipeline.application import final_result as _fr
from meshpipeline.application import job_cancel
from meshpipeline.persistence.job_state import TransitionResult
from meshpipeline.persistence.models import JobStatus

JOB = uuid.uuid4()
OWNER = "engineer@example.com"
ORG = str(uuid.uuid4())

#: What the durable checkpoint of job 6ddb58aa actually holds, read off `checkpoints`.
MEASURED_STATE = {
    "engine": "cfmesh_conjugate",
    "purpose": "conjugate_heat_transfer",
    "dimensionality": "3D",
    "executor_success": False,
    "executor_failed_gate": "quality_floor",
    "executor_failure_cause": "mesh_quality",
    "retry_count": 1,
}


class _Session:
    def __init__(self, log): self.log = log
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def commit(self): self.log.append(("commit",))


def _row():
    return SimpleNamespace(
        id=JOB, owner_id=OWNER, status=JobStatus.running,
        active_worker_token=uuid.uuid4(), execution_generation=1, current_attempt=1,
        cancel_reason=None,
        dispatch_payload={"mesh_engine": "cfmesh_conjugate",
                          "purpose": "conjugate_heat_transfer",
                          "dimensionality": "3D", "approved_snapshot_id": "snap-1"})


class _Jobs:
    def __init__(self, row, log): self.row = row; self.log = log; self.final = None

    async def lock_for_owner(self, db, job_id, owner_id, *, organization_id=""):
        return self.row

    async def transition(self, db, job_id, target, *, allow=None):
        self.row.status = target
        return TransitionResult.applied

    async def set_final_result(self, db, job_id, final_result):
        self.final = final_result


class _Lease:
    def __init__(self, row): self.row = row

    async def evict_owner(self, db, job_id, *, now=None):
        self.row.active_worker_token = None
        return True


class _Outbox:
    def __init__(self): self.rows = []

    async def enqueue(self, db, **kw):
        self.rows.append(kw)
        return True


@pytest.fixture
def wired(monkeypatch):
    """The cancel's after-the-commit dependencies, stubbed; nothing else is touched."""
    class _Launcher:
        async def revoke(self, job_id): return None

    import meshpipeline.contracts.pipeline_execution as pe
    monkeypatch.setattr(pe, "_launcher", _Launcher())

    import meshpipeline.application.outbox_publisher as obp

    async def _deliver(session_factory, job_id, *, repo=None):
        return obp.PublishStats(published=1)
    monkeypatch.setattr(obp, "deliver_own_terminal_event", _deliver)
    return monkeypatch


async def _cancel(wired, *, state=MEASURED_STATE):
    """A cancel of a running job whose durable checkpoint holds `state`."""
    async def _read(job_id, *, generation):
        assert str(job_id) == str(JOB) and generation == 1
        return dict(state)
    wired.setattr(job_cancel, "read_checkpoint_state", _read)
    row = _row()
    log: list = []
    jobs, outbox = _Jobs(row, log), _Outbox()
    out = await job_cancel.cancel_job(
        JOB, owner_id=OWNER, organization_id=ORG,
        session_factory=lambda: _Session(log), job_repo=jobs,
        lease_repo=_Lease(row), outbox_repo=outbox)
    return out, jobs, outbox


# -- the record keeps what the run established ---------------------------------------------

def test_the_cancelled_record_can_carry_a_gate_at_all():
    """`build_cancelled_result` has no parameter for it, so nothing upstream could pass it."""
    r = _fr.build_cancelled_result(
        job_id=str(JOB), owner_id=OWNER, engine="cfmesh_conjugate",
        failed_gate="quality_floor", failure_cause="mesh_quality")
    assert r.failed_gate == "quality_floor"
    assert r.failure_cause == "mesh_quality"


def test_a_cancel_with_nothing_established_still_records_nothing():
    """A job cancelled before any gate ran has no gate. Absence stays absence."""
    r = _fr.build_cancelled_result(job_id=str(JOB), owner_id=OWNER, engine="cfmesh")
    assert r.failed_gate == "" and r.failure_cause == ""


def test_a_recorded_gate_does_not_turn_a_cancel_into_a_failure():
    """The owner asked; nothing broke. The taxonomy must not acquire a cancelled failure."""
    r = _fr.build_cancelled_result(
        job_id=str(JOB), owner_id=OWNER, engine="cfmesh_conjugate",
        failed_gate="quality_floor", failure_cause="mesh_quality")
    assert r.failure_category is None
    assert r.outcome_code == "cancelled"
    assert r.executor_success is False
    assert _fr.render_message(r).startswith("Cancelled by you."), (
        "the closing line reads the status, never the gate")
    assert "quality_floor" not in _fr.render_message(r)


# -- and the cancel authority asks for it ---------------------------------------------------

async def test_a_cancel_mid_run_records_the_gate_the_run_had_already_failed(wired):
    """Job 6ddb58aa, exactly: quality_floor was in the checkpoint and "" in the record."""
    out, jobs, outbox = await _cancel(wired)
    assert out.result is job_cancel.CancelResult.cancelled
    assert jobs.final["status"] == "cancelled"
    assert jobs.final["failed_gate"] == "quality_floor"
    assert jobs.final["failure_cause"] == "mesh_quality"
    assert jobs.final["engine"] == "cfmesh_conjugate"
    (enq,) = outbox.rows
    assert enq["event_payload"]["final_result"] == jobs.final, (
        "the announcement and the record have to be one truth")


async def test_a_cancel_before_any_gate_records_an_empty_one(wired):
    out, jobs, _ = await _cancel(wired, state={"engine": "cfmesh_conjugate",
                                               "executor_success": False, "retry_count": 0})
    assert out.result is job_cancel.CancelResult.cancelled
    assert jobs.final["failed_gate"] == "" and jobs.final["failure_cause"] == ""


async def test_an_unreadable_checkpoint_never_fails_the_cancel(wired):
    """The cancel is the owner's and must not depend on a read. An unreachable checkpoint store
    costs the gate's name, not the cancel."""
    async def _boom(job_id, *, generation):
        raise ConnectionError("checkpoint store down")
    wired.setattr(job_cancel, "read_checkpoint_state", _boom)
    row = _row()
    log: list = []
    jobs = _Jobs(row, log)
    out = await job_cancel.cancel_job(
        JOB, owner_id=OWNER, organization_id=ORG,
        session_factory=lambda: _Session(log), job_repo=jobs,
        lease_repo=_Lease(row), outbox_repo=_Outbox())
    assert out.result is job_cancel.CancelResult.cancelled
    assert row.status is JobStatus.cancelled
    assert jobs.final["failed_gate"] == ""
    assert ("commit",) in log


async def test_the_thread_the_cancel_reads_is_this_generations(wired):
    """A checkpoint thread is namespaced by state-schema version AND execution generation
    (pipeline_run: f"{job_id}:s{STATE_SCHEMA_VERSION}:g{generation}"). Reading another
    generation's thread would report a fact from a run this row no longer owns."""
    from meshpipeline.contracts.pipeline_state import STATE_SCHEMA_VERSION
    seen: list = []

    async def _read(job_id, *, generation):
        seen.append((str(job_id), generation))
        return dict(MEASURED_STATE)
    wired.setattr(job_cancel, "read_checkpoint_state", _read)
    row = _row()
    row.execution_generation = 4
    log: list = []
    jobs = _Jobs(row, log)
    await job_cancel.cancel_job(
        JOB, owner_id=OWNER, organization_id=ORG,
        session_factory=lambda: _Session(log), job_repo=jobs,
        lease_repo=_Lease(row), outbox_repo=_Outbox())
    assert seen == [(str(JOB), 4)]
    assert job_cancel.checkpoint_thread(str(JOB), 4) == \
        f"{JOB}:s{STATE_SCHEMA_VERSION}:g4"
