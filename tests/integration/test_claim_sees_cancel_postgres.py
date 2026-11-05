# Responsibility: Verify an owner's cancel that commits after the worker first read the job stops the worker's claim.
# Boundaries: the claim and the cancel as production runs them, on real PostgreSQL; the lock-read rule
#             for every repository method is tests/unit/persistence/test_locked_job_reads_are_fresh.py.
from __future__ import annotations

import logging
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests import harness_provisioning as hp

import meshpipeline.settings.providers as provcfg
from meshpipeline.application import execution_fence as fence
from meshpipeline.application.job_cancel import CancelResult, cancel_job
from meshpipeline.persistence.lease import ClaimResult, LeaseRepository
from meshpipeline.persistence.models import JobStatus, SimulationJob
from meshpipeline.persistence.repositories.job_repository import JobRepository

OWNER = "owner-claim-race"


@pytest.fixture()
async def SessionLocal():
    connect_args = {"ssl": True} if provcfg.DB_SSL_REQUIRED else {}
    try:
        engine = create_async_engine(provcfg.POSTGRES_DSN, connect_args=connect_args, pool_size=8)
        await hp.reset_schema(provcfg.POSTGRES_DSN)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"PostgreSQL not reachable - PROVISION it ({type(exc).__name__}: {exc})")
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _pending_job(SessionLocal) -> uuid.UUID:
    async with SessionLocal() as s:
        job = SimulationJob(owner_id=OWNER, status=JobStatus.pending)
        s.add(job)
        await s.commit()
        return job.id


async def _cancel(SessionLocal, jid) -> None:
    outcome = await cancel_job(jid, owner_id=OWNER, reason="changed my mind",
                               session_factory=SessionLocal)
    assert outcome.result is CancelResult.cancelled


async def _assert_still_cancelled(SessionLocal, jid) -> None:
    async with SessionLocal() as s:
        row = await s.get(SimulationJob, jid)
        assert row.status is JobStatus.cancelled, f"the claim resurrected the job as {row.status}"
        assert row.cancel_reason == "changed my mind"
        assert (row.final_result or {}).get("status") == "cancelled"
        assert row.active_worker_token is None and row.started_at is None
        assert row.execution_generation == 0
        # the symptom the owner saw: a resurrected job still held one of their active slots
        assert await JobRepository().count_active_for_owner(s, OWNER) == 0


async def test_a_cancel_that_lands_after_the_workers_first_read_stops_the_delivery(SessionLocal):
    jid = await _pending_job(SessionLocal)

    class _OwnerCancelsAfterTheRead(JobRepository):
        async def get_internal(self, db, job_id):
            row = await super().get_internal(db, job_id)
            await _cancel(SessionLocal, job_id)      # its own session, committed before the claim
            return row

    outcome = await fence.claim_delivery(SessionLocal, _OwnerCancelsAfterTheRead(), str(jid),
                                         jlog=logging.getLogger(__name__), backend="celery",
                                         backend_execution_id="exec-A")

    assert isinstance(outcome, fence.DeliveryRefused), "the worker claimed a cancelled job"
    assert outcome.detail.get("skipped") == "already_terminal"
    await _assert_still_cancelled(SessionLocal, jid)


async def test_the_claim_rereads_a_row_its_own_session_already_holds(SessionLocal):
    jid = await _pending_job(SessionLocal)
    async with SessionLocal() as worker:
        seen = await JobRepository().get_internal(worker, jid)
        assert seen.status is JobStatus.pending
        await _cancel(SessionLocal, jid)
        result, ownership = await LeaseRepository().claim_execution(
            worker, jid, worker_token=uuid.uuid4(), backend="celery",
            backend_execution_id="exec-A")
        await worker.commit()

    assert (result, ownership) == (ClaimResult.already_terminal, None)
    assert seen.status is JobStatus.cancelled, "the locked read left the pre-lock copy in place"
    await _assert_still_cancelled(SessionLocal, jid)
