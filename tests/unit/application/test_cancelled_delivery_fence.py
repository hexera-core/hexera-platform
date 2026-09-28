# Responsibility: Verify a worker that passed its ownership check before a cancel committed still registers no artifact, announces nothing and is never charged.
# Boundaries: the registration fence, its wiring into the orchestrator, and what the fenced run leaves; the cancel's own order is test_job_cancel.
from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace

import pytest

from meshpipeline.application import artifact_uploader as AU
from meshpipeline.application import execution_fence as fence
from meshpipeline.persistence import lease as lease_mod
from meshpipeline.persistence.lease import ExecutionOwnership
from meshpipeline.persistence.models import ArtifactType, JobStatus

JOB = uuid.uuid4()
TOKEN = uuid.uuid4()
OWN = ExecutionOwnership(job_id=JOB, execution_generation=1, worker_token=TOKEN,
                         backend="celery", pipeline_deadline_at=None)


class _Log:
    def __init__(self): self.warnings = []; self.errors = []
    def info(self, *a, **k): pass
    def warning(self, *a, **k): self.warnings.append(a)
    def error(self, *a, **k): self.errors.append(a)


def _lease_answering(row):
    class _Lease:
        locked: list = []

        async def lock_current_owner(self, db, own):
            _Lease.locked.append((db, own))
            return row
    return _Lease


# the fence itself

async def test_with_no_ownership_bound_there_is_nothing_to_check(monkeypatch):
    monkeypatch.setattr(lease_mod, "LeaseRepository", _lease_answering(None))
    await fence.lock_ownership_for_commit(object(), "artifact registration")   # no raise


async def test_an_evicted_token_is_refused_under_the_lock(monkeypatch):
    lease = _lease_answering(None)          # lock_current_owner: generation/token no longer match
    monkeypatch.setattr(lease_mod, "LeaseRepository", lease)
    db = object()
    with fence.execution_ownership(OWN):
        with pytest.raises(fence.StaleWorkerFenced):
            await fence.lock_ownership_for_commit(db, "artifact registration")
    assert lease.locked == [(db, OWN)], "the check did not take the row lock in the caller's transaction"


@pytest.mark.parametrize("status", [JobStatus.cancelled, JobStatus.succeeded, JobStatus.failed])
async def test_a_terminal_job_is_refused_even_with_a_matching_token(monkeypatch, status):
    monkeypatch.setattr(lease_mod, "LeaseRepository", _lease_answering(SimpleNamespace(status=status)))
    with fence.execution_ownership(OWN):
        with pytest.raises(fence.StaleWorkerFenced):
            await fence.lock_ownership_for_commit(object(), "artifact registration")


async def test_the_current_owner_passes(monkeypatch):
    monkeypatch.setattr(lease_mod, "LeaseRepository",
                        _lease_answering(SimpleNamespace(status=JobStatus.running)))
    with fence.execution_ownership(OWN):
        await fence.lock_ownership_for_commit(object(), "artifact registration")


# the delivery: the cancel commits between the orchestrator's check and the registration commit

class _Session:
    commits = 0

    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def commit(self):
        _Session.commits += 1


class _Pub:
    calls: list = []

    def __init__(self, job_id): self.job_id = job_id
    def note(self, msg, op_id=""): _Pub.calls.append(("note", op_id))
    def error(self, msg, op_id=""): _Pub.calls.append(("error", op_id))


def _stored_report():
    orphan = AU.OrphanObject(object_key=f"jobs/{JOB}/mesh.tar.gz", checksum="abc", size_bytes=10,
                             logical_key="mesh_bundle", artifact_type=ArtifactType.mesh_bundle,
                             delivery_attempt=0)
    return SimpleNamespace(delivered=[{"type": "mesh_bundle", "storage_key": orphan.object_key}],
                           stored=[orphan], orphans=[], required_all_delivered=lambda: True)


@pytest.fixture
def delivery(monkeypatch):
    _Session.commits = 0
    _Pub.calls = []
    uploads = {"n": 0}
    persisted: list = []

    async def _upload(*a, **k):
        uploads["n"] += 1
        return _stored_report()
    monkeypatch.setattr(AU, "upload_job_artifacts", _upload)

    async def _persist(owner_id, job_id, orphans):
        persisted.extend(orphans)

    class _NoSleep:
        @staticmethod
        async def sleep(_s): return None
    monkeypatch.setattr(AU, "asyncio", _NoSleep)
    return {"uploads": uploads, "persisted": persisted, "persist": _persist}


async def _deliver(delivery, fence_commit):
    return await AU.deliver_succeeded_run(
        lambda: _Session(), job_id=str(JOB), owner_id="o", workspace="/ws", engine="cfmesh",
        delivery_attempt=0, execution_generation=1, jlog=_Log(), publish=_Pub,
        persist_orphans=delivery["persist"], fence_commit=fence_commit)


async def test_a_fenced_registration_commits_nothing_and_hands_its_uploads_to_the_reconciler(
        delivery, monkeypatch):
    monkeypatch.setattr(lease_mod, "LeaseRepository", _lease_answering(None))   # the cancel won

    async def _fence(db):
        await fence.lock_ownership_for_commit(db, "artifact registration")

    with fence.execution_ownership(OWN):
        with pytest.raises(fence.StaleWorkerFenced):
            await _deliver(delivery, _fence)

    assert _Session.commits == 0, "a fenced worker's artifact rows were committed"
    assert delivery["uploads"]["n"] == 1, "a fence refusal was retried as if it were a blip"
    assert [o.object_key for o in delivery["persisted"]] == [f"jobs/{JOB}/mesh.tar.gz"], (
        "the stored objects were not handed to the reconciler")
    assert _Pub.calls == [], f"a fenced worker announced something: {_Pub.calls}"


async def test_the_current_owner_still_delivers(delivery, monkeypatch):
    monkeypatch.setattr(lease_mod, "LeaseRepository",
                        _lease_answering(SimpleNamespace(status=JobStatus.running)))

    async def _fence(db):
        await fence.lock_ownership_for_commit(db, "artifact registration")

    with fence.execution_ownership(OWN):
        out = await _deliver(delivery, _fence)
    assert out.succeeded and _Session.commits == 1
    assert delivery["persisted"] == []
    assert ("note", "delivered") in _Pub.calls


async def test_the_fence_runs_before_the_commit_not_after(delivery):
    order: list = []

    class _Recording(_Session):
        async def commit(self):
            order.append("commit")

    async def _fence(db):
        order.append("fence")

    await AU.deliver_succeeded_run(
        lambda: _Recording(), job_id=str(JOB), owner_id="o", workspace="/ws", engine="cfmesh",
        delivery_attempt=0, execution_generation=1, jlog=_Log(), publish=_Pub,
        persist_orphans=delivery["persist"], fence_commit=_fence)
    assert order == ["fence", "commit"]


# the orchestrator wires it, and a fenced delivery ends like a fenced pre-check

def test_the_orchestrator_hands_the_registration_fence_to_the_delivery():
    import meshpipeline.application.pipeline_run as pr

    src = inspect.getsource(pr)
    call = src[src.index("_delivery = await deliver_succeeded_run("):]
    call = call[:call.index(")\n")]
    assert "fence_commit=_fence_registration" in call, "delivery runs without the registration fence"
    assert "lock_ownership_for_commit(db, " in src


def test_a_fenced_delivery_returns_fenced_with_no_terminal_work():
    import meshpipeline.application.pipeline_run as pr

    src = inspect.getsource(pr)
    start = src.index("_delivery = await deliver_succeeded_run(")
    handler = src[start:src.index("_uploaded_artifacts = _delivery.delivered", start)]
    assert "except StaleWorkerFenced:" in handler
    assert '"status": "fenced"' in handler, "a fenced delivery falls through to the terminal path"
    for forbidden in ("assemble_and_finalize", "finalize_terminal_atomic", "record_dead_letter",
                      "apply_delivery"):
        assert forbidden not in handler, f"the fenced branch still does {forbidden}"

