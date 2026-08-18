# Responsibility: Verify exactly one worker claims a job, and an expired lease is taken over with a bumped generation.
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests import harness_provisioning as hp

import meshpipeline.settings.providers as provcfg
from meshpipeline.persistence.lease import ClaimResult, LeaseRepository
from meshpipeline.persistence.models import JobStatus, SimulationJob

lease = LeaseRepository()


@pytest.fixture()
async def SessionLocal():
    connect_args = {"ssl": True} if provcfg.DB_SSL_REQUIRED else {}
    try:
        engine = create_async_engine(provcfg.POSTGRES_DSN, connect_args=connect_args, pool_size=8)
        # Alembic is the only thing that creates this schema - see
        # tests/harness_provisioning.py. `create_all` built tables no migration
        # had produced, so a suite could pass against a schema production never has.
        await hp.reset_schema(provcfg.POSTGRES_DSN)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"PostgreSQL not reachable - PROVISION it ({type(exc).__name__}: {exc})")
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _pending_job(SessionLocal) -> uuid.UUID:
    async with SessionLocal() as s:
        j = SimulationJob(owner_id="o", status=JobStatus.pending)
        s.add(j)
        await s.commit()
        return j.id


async def _claim(SessionLocal, job_id, *, token=None, backend="celery", exec_id=None, now=None):
    async with SessionLocal() as s:
        res, own = await lease.claim_execution(
            s, job_id, worker_token=token or uuid.uuid4(), backend=backend,
            backend_execution_id=exec_id, now=now)
        await s.commit()
        return res, own


async def _row(SessionLocal, job_id):
    async with SessionLocal() as s:
        return await s.get(SimulationJob, job_id)


# two initial workers race
async def test_two_initial_claims_exactly_one_wins(SessionLocal):
    job = await _pending_job(SessionLocal)
    t1, t2 = uuid.uuid4(), uuid.uuid4()
    (r1, o1), (r2, o2) = await asyncio.gather(
        _claim(SessionLocal, job, token=t1), _claim(SessionLocal, job, token=t2))
    results = {r1, r2}
    assert results == {ClaimResult.acquired_new_generation, ClaimResult.active_lease_conflict}
    row = await _row(SessionLocal, job)
    assert row.status == JobStatus.running
    assert row.execution_generation == 1                        # exactly ONE generation
    assert row.active_worker_token in (t1, t2)
    winner = o1 or o2
    assert winner and winner.execution_generation == 1


# duplicate claim while lease active
async def test_duplicate_claim_while_lease_active_is_conflict(SessionLocal):
    job = await _pending_job(SessionLocal)
    r1, o1 = await _claim(SessionLocal, job, token=uuid.uuid4())
    assert r1 == ClaimResult.acquired_new_generation
    r2, o2 = await _claim(SessionLocal, job, token=uuid.uuid4())     # different worker, lease still valid
    assert r2 == ClaimResult.active_lease_conflict and o2 is None
    row = await _row(SessionLocal, job)
    assert row.execution_generation == 1                             # NO increment, NO token replace
    assert row.active_worker_token == o1.worker_token


# expired-lease takeover: different execution -> new generation
async def test_expired_lease_takeover_different_execution_bumps_generation(SessionLocal):
    job = await _pending_job(SessionLocal)
    past = datetime.now(UTC) - timedelta(hours=2)
    r1, o1 = await _claim(SessionLocal, job, token=uuid.uuid4(), backend="celery",
                          exec_id="exec-A", now=past)          # lease acquired 2h ago → expired now
    assert r1 == ClaimResult.acquired_new_generation and o1.execution_generation == 1
    r2, o2 = await _claim(SessionLocal, job, token=uuid.uuid4(), backend="cloudrun_job",
                          exec_id="exec-B")                    # DIFFERENT execution takes over
    assert r2 == ClaimResult.acquired_new_generation and o2.execution_generation == 2
    row = await _row(SessionLocal, job)
    assert row.execution_generation == 2 and row.active_worker_token == o2.worker_token
    # old token is fenced: a heartbeat on gen-1 fails
    async with SessionLocal() as s:
        assert await lease.heartbeat(s, o1) is False


# expired-lease takeover: SAME execution restart -> same generation, rotated token
async def test_expired_lease_same_execution_resumes_generation(SessionLocal):
    job = await _pending_job(SessionLocal)
    past = datetime.now(UTC) - timedelta(hours=2)
    r1, o1 = await _claim(SessionLocal, job, token=uuid.uuid4(), exec_id="exec-A", now=past)
    assert o1.execution_generation == 1
    r2, o2 = await _claim(SessionLocal, job, token=uuid.uuid4(), exec_id="exec-A")   # SAME exec id
    assert r2 == ClaimResult.resumed_same_generation and o2.execution_generation == 1  # gen UNCHANGED
    row = await _row(SessionLocal, job)
    assert row.active_worker_token == o2.worker_token and o2.worker_token != o1.worker_token  # rotated
    async with SessionLocal() as s:
        assert await lease.heartbeat(s, o1) is False           # old token still fenced


# heartbeat fencing
async def test_current_owner_heartbeats_stale_owner_cannot(SessionLocal):
    job = await _pending_job(SessionLocal)
    _, o1 = await _claim(SessionLocal, job, token=uuid.uuid4())
    async with SessionLocal() as s:
        assert await lease.heartbeat(s, o1) is True
        await s.commit()
    # fabricate a stale ownership (wrong token) - cannot revive
    from meshpipeline.persistence.lease import ExecutionOwnership
    stale = ExecutionOwnership(job_id=job, execution_generation=1, worker_token=uuid.uuid4(),
                               backend="celery", pipeline_deadline_at=None)
    async with SessionLocal() as s:
        assert await lease.heartbeat(s, stale) is False


# pipeline deadline anchored once, never reset on takeover
async def test_pipeline_deadline_anchored_once_and_not_reset_on_takeover(SessionLocal):
    job = await _pending_job(SessionLocal)
    past = datetime.now(UTC) - timedelta(hours=2)
    _, o1 = await _claim(SessionLocal, job, token=uuid.uuid4(), exec_id="exec-A", now=past)
    row1 = await _row(SessionLocal, job)
    deadline1 = row1.pipeline_deadline_at
    assert deadline1 is not None
    _, o2 = await _claim(SessionLocal, job, token=uuid.uuid4(), exec_id="exec-B")   # takeover
    row2 = await _row(SessionLocal, job)
    assert row2.pipeline_deadline_at == deadline1                # UNCHANGED across takeover/generation


# terminal job cannot be claimed
async def test_terminal_job_cannot_be_claimed(SessionLocal):
    job = await _pending_job(SessionLocal)
    async with SessionLocal() as s:
        row = await s.get(SimulationJob, job)
        row.status = JobStatus.succeeded
        await s.commit()
    r, o = await _claim(SessionLocal, job, token=uuid.uuid4())
    assert r == ClaimResult.already_terminal and o is None
