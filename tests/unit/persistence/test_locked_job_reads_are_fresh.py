# Responsibility: Verify every row-locked job read decides on the row as the lock found it, not as the session last saw it.
# Boundaries: the repositories against a session that already holds the row; the same race on real
#             PostgreSQL is tests/integration/test_claim_sees_cancel_postgres.py.
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from meshpipeline.application import execution_fence as fence
from meshpipeline.persistence.lease import ClaimResult, ExecutionOwnership, LeaseRepository
from meshpipeline.persistence.models import JobStatus, SimulationJob
from meshpipeline.persistence.repositories.job_repository import JobRepository

JOB = uuid.uuid4()
OWNER = "owner-1"


class _Result:
    def __init__(self, row): self._row = row
    def scalar_one_or_none(self): return self._row


class _Session:
    """An AsyncSession reduced to the part this defect lives in: one identity map over one row.

    SQLAlchemy's rule for a SELECT that finds a row the session already holds: it returns THAT
    instance with the attributes it already had, FOR UPDATE or not. Only a statement carrying
    `populate_existing` overwrites them from the row just read. The rule is applied to the
    statement the repository actually built. `committed` is the database row, which another
    transaction's commit changes; a flush writes the instance back over it.
    """

    def __init__(self, committed: dict):
        self.committed = committed
        self.instance: SimulationJob | None = None

    async def __aenter__(self): return self
    async def __aexit__(self, *exc): return False

    async def execute(self, statement):
        if self.instance is None:
            self.instance = SimulationJob(**self.committed)
        elif statement.get_execution_options().get("populate_existing"):
            for column, value in self.committed.items():
                setattr(self.instance, column, value)
        return _Result(self.instance)

    async def flush(self):
        if self.instance is not None:
            for column in self.committed:
                self.committed[column] = getattr(self.instance, column)

    async def commit(self):
        await self.flush()


def _row(status=JobStatus.pending, token=None, generation=0):
    now = datetime.now(UTC)
    return {"id": JOB, "owner_id": OWNER, "status": status, "created_at": now,
            "execution_generation": generation, "execution_claim_epoch": generation,
            "active_worker_token": token, "owning_backend": "celery" if token else None,
            "pipeline_execution_id": None, "lease_acquired_at": now if token else None,
            "lease_heartbeat_at": now if token else None,
            "lease_expires_at": now + timedelta(seconds=900) if token else None,
            "started_at": now if token else None,
            "pipeline_deadline_at": now + timedelta(hours=6) if token else None,
            "cancel_reason": None}


def _cancel(committed: dict) -> None:
    # What job_cancel.cancel_job commits: the holder evicted, the status terminal.
    committed.update(status=JobStatus.cancelled, active_worker_token=None,
                     lease_expires_at=datetime.now(UTC), cancel_reason="changed my mind")


def _own(token, generation=1):
    return ExecutionOwnership(job_id=JOB, execution_generation=generation, worker_token=token,
                              backend="celery", pipeline_deadline_at=None)


async def _held(committed: dict) -> _Session:
    # A session that has already read the row once, the way claim_delivery's get_internal does.
    db = _Session(committed)
    await JobRepository().get_internal(db, JOB)
    return db


class _Log:
    def __init__(self): self.lines = []
    def info(self, *a, **k): self.lines.append(("info", a))
    def warning(self, *a, **k): self.lines.append(("warning", a))
    def error(self, *a, **k): self.lines.append(("error", a))


# the incident: a cancel committed between the worker's read and its claim

async def test_a_cancel_landing_after_the_read_refuses_the_delivery(monkeypatch):
    from tests.execution_fence_double import install

    mirror = install(monkeypatch)
    committed = _row()

    class _CancelLandsAfterTheRead(JobRepository):
        async def get_internal(self, db, job_id):
            row = await super().get_internal(db, job_id)
            _cancel(committed)       # the owner's cancel commits on its own connection
            return row

    outcome = await fence.claim_delivery(lambda: _Session(committed), _CancelLandsAfterTheRead(),
                                         str(JOB), jlog=_Log(), backend="celery",
                                         backend_execution_id="exec-A")

    assert isinstance(outcome, fence.DeliveryRefused), "the worker claimed a job its owner cancelled"
    assert outcome.detail.get("skipped") == "already_terminal"
    assert committed["status"] is JobStatus.cancelled, "the claim wrote over the cancel"
    assert committed["active_worker_token"] is None
    assert committed["started_at"] is None and committed["execution_generation"] == 0
    assert mirror.calls == [], "a fence was installed for a cancelled job"


async def test_the_claim_decides_on_the_status_the_lock_found():
    committed = _row()
    db = await _held(committed)
    _cancel(committed)

    result, ownership = await LeaseRepository().claim_execution(
        db, JOB, worker_token=uuid.uuid4(), backend="celery", backend_execution_id="exec-A")
    await db.commit()

    assert (result, ownership) == (ClaimResult.already_terminal, None)
    assert committed["status"] is JobStatus.cancelled


# every other locked read, each against the commit that would fool it

async def test_the_heartbeat_does_not_renew_a_lease_the_cancel_withdrew():
    token = uuid.uuid4()
    committed = _row(JobStatus.running, token, generation=1)
    db = await _held(committed)
    _cancel(committed)
    withdrawn_at = committed["lease_expires_at"]

    assert await LeaseRepository().heartbeat(db, _own(token)) is False
    await db.commit()
    assert committed["lease_expires_at"] == withdrawn_at
    assert committed["active_worker_token"] is None


async def test_the_terminal_lock_refuses_an_evicted_owner():
    token = uuid.uuid4()
    committed = _row(JobStatus.running, token, generation=1)
    db = await _held(committed)
    _cancel(committed)

    assert await LeaseRepository().lock_current_owner(db, _own(token)) is None


async def test_the_eviction_sees_a_worker_that_claimed_after_the_read(monkeypatch):
    from tests.execution_fence_double import install

    install(monkeypatch)
    committed = _row()
    db = await _held(committed)
    committed.update(_row(JobStatus.running, uuid.uuid4(), generation=1))

    assert await LeaseRepository().evict_owner(db, JOB) is True
    await db.commit()
    assert committed["active_worker_token"] is None, "the worker kept a claim the cancel missed"


async def test_a_release_leaves_the_new_owners_claim_alone(monkeypatch):
    from tests.execution_fence_double import install

    install(monkeypatch)
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    committed = _row(JobStatus.running, mine, generation=1)
    db = await _held(committed)
    committed.update(active_worker_token=theirs, execution_generation=2)     # a takeover

    await LeaseRepository().release(db, _own(mine))
    await db.commit()
    assert committed["active_worker_token"] == theirs, "a superseded worker released the new owner"
    assert committed["execution_generation"] == 2


async def test_the_owners_lock_sees_a_finish_committed_after_the_read():
    committed = _row(JobStatus.running, uuid.uuid4(), generation=1)
    db = await _held(committed)
    committed.update(status=JobStatus.succeeded)

    row = await JobRepository().lock_for_owner(db, JOB, OWNER)
    assert row is not None and row.status is JobStatus.succeeded
