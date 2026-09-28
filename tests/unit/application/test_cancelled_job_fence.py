# Responsibility: Verify a cancelled job's late worker result is refused at every fence, never delivered and never charged.
# Boundaries: the fences and the counters a cancel relies on; the cancel's own order is test_job_cancel.
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from meshpipeline.persistence.job_state import TransitionResult
from meshpipeline.persistence.lease import ExecutionOwnership, LeaseRepository
from meshpipeline.persistence.models import JobStatus

JOB = uuid.uuid4()


class _Result:
    def __init__(self, row=None, rowcount=0):
        self._row = row
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self._row


def _row(status, token, generation=1):
    now = datetime.now(UTC)
    return SimpleNamespace(id=JOB, status=status, execution_generation=generation,
                           active_worker_token=token, lease_heartbeat_at=now,
                           lease_expires_at=now + timedelta(seconds=900),
                           pipeline_deadline_at=now + timedelta(hours=6))


def _own(token, generation=1):
    return ExecutionOwnership(job_id=JOB, execution_generation=generation, worker_token=token,
                              backend="celery", pipeline_deadline_at=None)


def _db(row):
    return SimpleNamespace(execute=AsyncMock(return_value=_Result(row)), flush=AsyncMock())


# the row-level fences the worker passes before any side effect

async def test_a_cancelled_row_is_never_owned_even_by_the_token_it_still_names():
    token = uuid.uuid4()
    # status alone decides: even a row whose token was somehow left in place refuses
    owned = await LeaseRepository().is_current_owner(_db(_row(JobStatus.cancelled, token)), _own(token))
    assert owned is False


async def test_an_evicted_token_fails_the_atomic_terminal_lock():
    token = uuid.uuid4()
    locked = await LeaseRepository().lock_current_owner(_db(_row(JobStatus.running, None)), _own(token))
    assert locked is None


async def test_the_heartbeat_cannot_heal_a_fence_after_a_cancel(monkeypatch):
    from tests.execution_fence_double import install

    fence = install(monkeypatch)
    db = _db(_row(JobStatus.cancelled, None))
    assert await LeaseRepository().heartbeat(db, _own(uuid.uuid4())) is False
    db.flush.assert_not_awaited()
    assert "heal" not in fence.operations() and "refresh" not in fence.operations()


# the eviction itself

async def test_evict_revokes_exactly_the_holders_fence_then_clears_the_token(monkeypatch):
    from tests.execution_fence_double import install

    fence = install(monkeypatch)
    token = uuid.uuid4()
    fence.install(str(JOB), fence.fingerprint(str(JOB), 1, token), 600)
    row = _row(JobStatus.running, token)
    db = _db(row)
    assert await LeaseRepository().evict_owner(db, JOB) is True
    assert row.active_worker_token is None
    assert fence.current(str(JOB)) == "", "the mirror still authorises the evicted worker"
    assert fence.operations()[-1] == "revoke"
    db.flush.assert_awaited()


async def test_evict_of_an_unclaimed_job_is_a_no_op(monkeypatch):
    from tests.execution_fence_double import install

    fence = install(monkeypatch)
    db = _db(_row(JobStatus.pending, None))
    assert await LeaseRepository().evict_owner(db, JOB) is False
    assert fence.calls == [] and db.flush.await_count == 0


async def test_evict_leaves_another_generations_fence_alone(monkeypatch):
    from tests.execution_fence_double import install

    fence = install(monkeypatch)
    other = fence.fingerprint(str(JOB), 2, uuid.uuid4())
    fence.install(str(JOB), other, 600)
    row = _row(JobStatus.running, uuid.uuid4(), generation=1)
    assert await LeaseRepository().evict_owner(_db(row), JOB) is True
    assert row.active_worker_token is None
    assert fence.current(str(JOB)) == other, "a fence that is not the holder's was removed"


async def test_a_mirror_outage_still_clears_the_token(monkeypatch):
    import meshpipeline.persistence.lease as mod

    class _Down:
        def fingerprint(self, *a): return "fp"
        def revoke(self, *a): raise ConnectionError("redis down")
    monkeypatch.setattr(mod, "_fence_ops", lambda: _Down())
    row = _row(JobStatus.running, uuid.uuid4())
    assert await LeaseRepository().evict_owner(_db(row), JOB) is True
    assert row.active_worker_token is None, "a Redis blip left the worker in ownership"


# the atomic terminal transaction: nothing written, nothing charged

class _Jobs:
    def __init__(self, current): self.current = current; self.calls = []

    async def transition(self, db, job_id, target, *, allow=None):
        self.calls.append(("transition", target))
        return TransitionResult.rejected_current_state

    async def get_internal(self, db, job_id):
        self.calls.append(("get_internal",))
        return SimpleNamespace(status=self.current, id=job_id)

    async def set_final_result(self, db, job_id, fr):
        self.calls.append(("set_final_result",))


class _Outbox:
    def __init__(self): self.rows = []

    async def enqueue(self, db, **kw):
        self.rows.append(kw)
        return True


class _LeaseFenced:
    async def lock_current_owner(self, db, own): return None


@pytest.fixture
def no_charge(monkeypatch):
    from meshpipeline.application import metering_service

    async def _boom(db, *, job):
        raise AssertionError("a cancelled job was charged")
    monkeypatch.setattr(metering_service, "charge_for_job", _boom)


def _late_success_dict():
    from meshpipeline.application.final_result import TerminalStatus, build_final_result

    return build_final_result(
        job_id=str(JOB), owner_id="o", status=TerminalStatus.succeeded, engine="cfmesh",
        purpose="external_cfd", dimensionality="3D", approved_snapshot_id="s",
        executor_success=True, reviewer_verdict="PASS", failed_gate="", api_failure="",
        attempts=1, attempts_max=4, required_ready=True, delivered_types=["mesh_bundle"],
        optional_warnings=[]).to_dict()


async def test_a_late_success_from_an_evicted_worker_is_fenced_before_any_write(no_charge):
    from meshpipeline.application.terminal_finalize import finalize_terminal_atomic

    jobs, outbox = _Jobs(JobStatus.cancelled), _Outbox()
    out = await finalize_terminal_atomic(
        SimpleNamespace(), ownership=_own(uuid.uuid4()), intended_status=JobStatus.succeeded,
        failed_reason=None, final_result_dict=_late_success_dict(), closing_message="Mesh ready",
        lease_repo=_LeaseFenced(), job_repo=jobs, outbox_repo=outbox)
    assert out.fenced is True and out.enqueued is False
    assert jobs.calls == [] and outbox.rows == [], "a fenced worker still wrote something"


async def test_an_unfenced_late_success_is_refused_by_the_transition_table(no_charge):
    # ownership=None skips the row lock: the CAS alone must still refuse a cancelled job
    from meshpipeline.application.terminal_finalize import finalize_terminal_atomic

    jobs, outbox = _Jobs(JobStatus.cancelled), _Outbox()
    out = await finalize_terminal_atomic(
        SimpleNamespace(), ownership=None, intended_status=JobStatus.succeeded,
        failed_reason=None, final_result_dict=_late_success_dict(), closing_message="Mesh ready",
        lease_repo=_LeaseFenced(), job_repo=jobs, outbox_repo=outbox)
    assert out.fenced is False and out.transition is TransitionResult.rejected_current_state
    assert out.durable_status is JobStatus.cancelled and out.enqueued is False
    assert ("set_final_result",) not in jobs.calls and outbox.rows == []


async def test_a_cancelled_job_costs_nothing():
    from meshpipeline.application.metering_service import charge_for_job

    job = SimpleNamespace(status=JobStatus.cancelled, organization_id=uuid.uuid4(), id=JOB,
                          started_at=datetime.now(UTC) - timedelta(hours=2),
                          ended_at=datetime.now(UTC))
    assert await charge_for_job(SimpleNamespace(), job=job) == 0


def test_a_cancelled_job_frees_the_owners_slot_and_the_credit_reserve():
    from meshpipeline.persistence.repositories.job_repository import JobRepository

    # both the per-owner quota and spend_gate's reservation count these statuses and no other
    assert JobStatus.cancelled not in JobRepository._ACTIVE_STATUSES


async def test_a_redelivered_cancelled_job_is_never_re_run():
    from meshpipeline.application import execution_fence as fence

    class _Repo:
        async def get_internal(self, db, jid):
            return SimpleNamespace(status=JobStatus.cancelled, owner_id="o", created_at=None)

    class _S:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def commit(self): return None

    class _Log:
        def info(self, *a, **k): pass
        def warning(self, *a, **k): pass
        def error(self, *a, **k): pass

    out = await fence.claim_delivery(_S, _Repo(), str(JOB), jlog=_Log(),
                                     backend="celery", backend_execution_id="x")
    assert isinstance(out, fence.DeliveryRefused) and out.detail["skipped"] == "already_terminal"


def test_the_purge_treats_a_cancelled_workspace_like_a_failed_one():
    import inspect

    from meshpipeline.application.maintenance import cleanup

    src = inspect.getsource(cleanup)
    assert "JobStatus.status.in_" not in src  # sanity: the filter is on the column
    assert "SimulationJob.status.in_([JobStatus.failed, JobStatus.cancelled])" in src, (
        "a cancelled run's workspace would never be purged")
