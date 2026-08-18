# Responsibility: Verify a job is claimed once, a poison job is dead-lettered, and the tenant comes from the row.
from __future__ import annotations

import uuid

import pytest

from meshpipeline.application import execution_fence as fence
from meshpipeline.persistence.lease import ClaimResult, ExecutionOwnership
from meshpipeline.persistence.models import JobStatus

JOB = str(uuid.uuid4())


class _Log:
    def __init__(self): self.lines = []
    def info(self, *a, **k): self.lines.append(("info", a))
    def warning(self, *a, **k): self.lines.append(("warning", a))
    def error(self, *a, **k): self.lines.append(("error", a))


class _Session:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def commit(self): return None


def _sessions():
    return _Session


class _Row:
    def __init__(self, status=JobStatus.pending):
        self.status = status
        self.owner_id = uuid.uuid4()
        self.created_at = "2026-08-03T00:00:00Z"
        self.failed_reason = None


class _Repo:
    def __init__(self, row=None): self.row = row; self.transitions = []
    async def get_internal(self, db, jid): return self.row
    async def transition(self, db, jid, status):
        from meshpipeline.persistence.job_state import TransitionResult
        self.transitions.append(status)
        return TransitionResult.applied


@pytest.fixture
def lease(monkeypatch):
    import meshpipeline.persistence.lease as mod

    state = {"result": ClaimResult.acquired_new_generation, "ownership": None}

    class _Fake:
        async def claim_execution(self, db, jid, **kw):
            own = state["ownership"]
            if own is None and state["result"] is ClaimResult.acquired_new_generation:
                own = ExecutionOwnership(job_id=jid, execution_generation=3,
                                         worker_token=kw["worker_token"], backend="direct",
                                         pipeline_deadline_at=None)
            return state["result"], own

    monkeypatch.setattr(mod, "LeaseRepository", _Fake)
    return state


# redelivery guard

async def test_a_delivery_under_the_cap_proceeds():
    assert await fence.guard_redelivery(_sessions(), _Repo(_Row()), JOB, jlog=_Log(),
                                        max_redeliveries=3, deliveries=3) is None


async def test_a_poison_job_is_failed_and_dead_lettered(monkeypatch):
    recorded = []
    import meshpipeline.errors as errs
    monkeypatch.setattr(errs, "record_dead_letter",
                        lambda *a, **k: recorded.append(a))
    repo = _Repo(_Row())
    refusal = await fence.guard_redelivery(_sessions(), repo, JOB, jlog=_Log(),
                                           max_redeliveries=3, deliveries=4)
    assert isinstance(refusal, fence.DeliveryRefused)
    assert refusal.detail["status"] == "failed"
    assert refusal.detail["reason"] == "redelivery_cap"
    assert JobStatus.failed in repo.transitions, "the poison job was never transitioned to failed"
    assert recorded, "no dead-letter record was written"


async def test_a_database_failure_still_dead_letters(monkeypatch):
    recorded = []
    import meshpipeline.errors as errs
    monkeypatch.setattr(errs, "record_dead_letter", lambda *a, **k: recorded.append(a))

    class _Boom(_Repo):
        async def transition(self, *a, **k): raise RuntimeError("db down")

    refusal = await fence.guard_redelivery(_sessions(), _Boom(_Row()), JOB, jlog=_Log(),
                                           max_redeliveries=1, deliveries=9)
    assert isinstance(refusal, fence.DeliveryRefused) and recorded


# the claim branch table

async def test_a_granted_claim_returns_ownership_and_the_row_timestamp(lease):
    out = await fence.claim_delivery(_sessions(), _Repo(_Row()), JOB, jlog=_Log(),
                                     backend="direct", backend_execution_id="x")
    assert isinstance(out, fence.DeliveryClaim)
    assert out.ownership.execution_generation == 3
    assert out.worker_token is not None
    assert out.job_created_at == "2026-08-03T00:00:00Z", (
        "the pipeline budget's origin was not carried out of the claim")


@pytest.mark.parametrize("status", [JobStatus.succeeded, JobStatus.failed])
async def test_an_already_terminal_job_is_never_re_run(lease, status):
    out = await fence.claim_delivery(_sessions(), _Repo(_Row(status)), JOB, jlog=_Log(),
                                     backend="direct", backend_execution_id="x")
    assert isinstance(out, fence.DeliveryRefused)
    assert out.detail["skipped"] == "already_terminal"
    assert out.detail["status"] == status.value


@pytest.mark.parametrize("result,reason", [
    (ClaimResult.not_found, "not_startable"),
    (ClaimResult.invalid_job_state, "not_startable"),
    (ClaimResult.active_lease_conflict, "active_lease_conflict"),
])
async def test_a_refused_claim_never_runs(lease, result, reason):
    lease["result"] = result
    out = await fence.claim_delivery(_sessions(), _Repo(_Row()), JOB, jlog=_Log(),
                                     backend="direct", backend_execution_id="x")
    assert isinstance(out, fence.DeliveryRefused)
    assert out.detail["status"] == "skipped" and out.detail["reason"] == reason


async def test_already_terminal_claim_result_is_skipped(lease):
    lease["result"] = ClaimResult.already_terminal
    out = await fence.claim_delivery(_sessions(), _Repo(_Row()), JOB, jlog=_Log(),
                                     backend="direct", backend_execution_id="x")
    assert isinstance(out, fence.DeliveryRefused) and out.detail["skipped"] == "already_terminal"


async def test_a_claim_granted_without_ownership_fails_closed(lease):
    lease["ownership"] = None
    lease["result"] = ClaimResult.acquired_same_generation \
        if hasattr(ClaimResult, "acquired_same_generation") else ClaimResult.acquired_new_generation

    import meshpipeline.persistence.lease as mod

    class _NoOwn:
        async def claim_execution(self, db, jid, **kw):
            return lease["result"], None
    mod.LeaseRepository = _NoOwn

    out = await fence.claim_delivery(_sessions(), _Repo(_Row()), JOB, jlog=_Log(),
                                     backend="direct", backend_execution_id="x")
    assert isinstance(out, fence.DeliveryRefused)
    assert out.detail["reason"] == "no_ownership", "a run proceeded without a proof of ownership"


async def test_the_tenant_is_bound_from_the_job_row_not_the_payload(lease, monkeypatch):
    bound = []
    from meshpipeline.capture import scope as _scope
    monkeypatch.setattr(_scope, "bind", lambda owner, job: bound.append((owner, job)))
    row = _Row()
    await fence.claim_delivery(_sessions(), _Repo(row), JOB, jlog=_Log(),
                               backend="direct", backend_execution_id="x")
    assert bound == [(str(row.owner_id), JOB)]


async def test_the_raw_worker_token_is_never_logged(lease):
    log = _Log()
    out = await fence.claim_delivery(_sessions(), _Repo(_Row()), JOB, jlog=log,
                                     backend="direct", backend_execution_id="x")
    joined = repr(log.lines)
    assert str(out.worker_token) not in joined, "the raw worker token was logged"


@pytest.fixture(autouse=True)
def _execution_fence(monkeypatch):
    # This suite claims delivery with no Redis. The mirror is stood in for at the two seams
    # production resolves; ownership checks, claim transitions and the transaction stay real.
    from tests.execution_fence_double import install
    return install(monkeypatch)
