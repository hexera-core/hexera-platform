# Responsibility: Verify the lease's hand-back gives a running job back to the queue only for the worker that holds it,
#                 and that a hand-back the queue refused lands where the reaper re-runs it.
# Boundaries: the SQL the two repository methods issue, read off a recording session; no database.
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Update

import meshpipeline.settings.runtime as rtcfg
from meshpipeline.persistence import lease as lease_mod
from meshpipeline.persistence.lease import ExecutionOwnership, LeaseRepository
from meshpipeline.persistence.models import JobStatus

NOW = datetime(2026, 9, 29, 6, 39, tzinfo=UTC)


class _Db:
    def __init__(self, rowcount=1, order=None):
        self.statements: list = []
        self.rowcount = rowcount
        self.order = order if order is not None else []

    async def execute(self, stmt):
        self.statements.append(stmt)
        self.order.append("update")
        return SimpleNamespace(rowcount=self.rowcount)


class _Fence:
    def __init__(self, order):
        self.order = order
        self.revoked: list[tuple[str, str]] = []

    def fingerprint(self, job_id, generation, token):
        return f"{job_id}:{generation}:{token}"

    def revoke(self, job_id, value):
        self.order.append("revoke")
        self.revoked.append((job_id, value))
        return True


def _own():
    return ExecutionOwnership(job_id=uuid.uuid4(), execution_generation=3,
                              worker_token=uuid.uuid4(), backend="celery",
                              pipeline_deadline_at=None)


def _values(stmt: Update) -> dict:
    return {col.name: getattr(bind, "value", bind) for col, bind in stmt._values.items()}


def _where(stmt: Update) -> tuple[str, dict]:
    compiled = stmt.whereclause.compile(dialect=postgresql.dialect())
    return str(compiled), dict(compiled.params)


@pytest.fixture
def fence(monkeypatch):
    order: list[str] = []
    f = _Fence(order)
    monkeypatch.setattr(lease_mod, "_fence_ops", lambda: f)
    return f


async def test_the_holder_gives_the_job_back_to_the_queue(fence):
    own, db = _own(), _Db(order=fence.order)
    assert await LeaseRepository().hand_back(db, own, now=NOW) is True

    (stmt,) = db.statements
    values = _values(stmt)
    # back to WAITING FOR A WORKER, owned by nobody: the next claim sees no live lease and takes
    # the job as a new execution generation
    assert values["status"] == JobStatus.pending and values["active_worker_token"] is None
    # released-but-not-yet-queued: the message goes out after this commits, and until the launch
    # is confirmed the reaper can find a job whose message never left
    assert values["lease_expires_at"] == NOW
    # the run's budget keeps counting from its first start, whatever machine it moves to
    assert "started_at" not in values and "pipeline_deadline_at" not in values
    sql, params = _where(stmt)
    # only the HOLDER: this generation AND this token, and only while running - a cancel or a
    # takeover that landed first leaves the row alone
    assert own.worker_token in params.values() and own.execution_generation in params.values()
    assert [JobStatus.running] in params.values()
    # the mirror goes first, while this worker is still the owner, so nothing of the stopped run
    # can be authorised by it afterwards
    assert fence.order == ["revoke", "update"]
    assert fence.revoked == [(str(own.job_id), fence.fingerprint(
        str(own.job_id), own.execution_generation, own.worker_token))]


async def test_a_worker_that_no_longer_holds_the_job_is_told_so(fence):
    assert await LeaseRepository().hand_back(_Db(rowcount=0, order=fence.order), _own(),
                                             now=NOW) is False


async def test_a_mirror_that_cannot_be_revoked_does_not_stop_the_hand_back(monkeypatch):
    class _Down(_Fence):
        def revoke(self, job_id, value):
            raise ConnectionError("redis down")
    monkeypatch.setattr(lease_mod, "_fence_ops", lambda: _Down([]))
    assert await LeaseRepository().hand_back(_Db(), _own(), now=NOW) is True


async def test_a_confirmed_requeue_clears_only_that_release(fence):
    own, db = _own(), _Db()
    assert await LeaseRepository().confirm_requeued(db, own.job_id, NOW) is True

    (stmt,) = db.statements
    values = _values(stmt)
    # the mark goes and nothing else moves - in particular not the status
    assert values == {"lease_expires_at": None}
    sql, params = _where(stmt)
    # the job exactly as that release left it: still pending, still unowned, that release's mark.
    # A worker that already claimed it, or a cancel, has moved it on and is not touched.
    assert [JobStatus.pending] in params.values()
    assert "active_worker_token IS NULL" in sql
    assert NOW in params.values()


def test_an_unconfirmed_release_is_what_the_reaper_looks_for():
    # The two halves must agree: the reaper selects an unowned pending job whose release mark is
    # older than a lease (application/maintenance/cleanup.py).
    from meshpipeline.application.maintenance import cleanup
    later = NOW + timedelta(seconds=int(rtcfg.WORKER_LEASE_SECONDS) + 60)
    sql = str(cleanup.stalled_jobs_clause(later).compile())
    assert ("active_worker_token IS NULL AND simulation_jobs.lease_expires_at IS NOT NULL "
            "AND simulation_jobs.lease_expires_at <") in sql, sql
