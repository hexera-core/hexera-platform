# Responsibility: Verify the publish-seam fence recovery heals a LAPSED mirror and never a lost claim,
# against real PostgreSQL and Redis.
# Boundaries: the one-recovery-then-fail-closed contract; the wider failure matrix stays in
# test_execution_fence_race / test_execution_fence_failures.
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import meshpipeline.settings.providers as provcfg
from meshpipeline.application.execution_fence import claim_delivery
from meshpipeline.contracts.event_stream import StaleExecutionPublish
from meshpipeline.events.channels import fence_key_for, log_key_for, opkey_set_for, seq_key_for
from meshpipeline.persistence.models import SimulationJob

pytestmark = pytest.mark.asyncio
if not os.getenv("DATABASE_URL"):
    pytest.skip("a real PostgreSQL endpoint is required", allow_module_level=True)

jlog = logging.getLogger(__name__)


def _sessions():
    engine = create_async_engine(provcfg.POSTGRES_DSN, pool_size=4, max_overflow=4,
                                 pool_pre_ping=True)
    return engine, async_sessionmaker(bind=engine, expire_on_commit=False)


def _client():
    import redis as _redis
    return _redis.from_url(provcfg.REDIS_URL)


def _backlog(job_id: str) -> int:
    r = _client()
    try:
        return r.llen(log_key_for(job_id))
    finally:
        r.close()


def _fence_pttl(job_id: str) -> int:
    r = _client()
    try:
        return r.pttl(fence_key_for(job_id))
    finally:
        r.close()


def _drop_fence(job_id: str) -> None:
    # the lapse: the mirror's TTL ran out between heartbeats while the claim stayed valid
    r = _client()
    try:
        r.delete(fence_key_for(job_id))
    finally:
        r.close()


async def _seed(job_id: uuid.UUID, owner_id: str) -> None:
    engine, Session = _sessions()
    try:
        async with Session() as db:
            db.add(SimulationJob(id=job_id, owner_id=owner_id))
            await db.commit()
    finally:
        await engine.dispose()


async def _claim(job_id: uuid.UUID, session_factory, *, backend_execution_id: str):
    from meshpipeline.persistence.repositories.job_repository import JobRepository
    return await claim_delivery(session_factory, JobRepository(), str(job_id), jlog=jlog,
                                backend="local", backend_execution_id=backend_execution_id)


def _gated(job_id: str):
    from meshpipeline.adapters.event_stream.redis import JobPublisher
    from meshpipeline.application.execution_publisher import OwnershipCheckedPublisher
    return OwnershipCheckedPublisher(JobPublisher(str(job_id), agent="builder"))


@pytest.fixture()
async def job():
    job_id = uuid.uuid4()
    await _seed(job_id, f"heal-{job_id.hex[:8]}")
    yield job_id
    r = _client()
    try:
        for key in (seq_key_for(str(job_id)), log_key_for(str(job_id)),
                    opkey_set_for(str(job_id)), fence_key_for(str(job_id))):
            r.delete(key)
    finally:
        r.close()


async def _set_row(job_id: uuid.UUID, **updates) -> None:
    engine, Session = _sessions()
    try:
        async with Session() as db:
            row = (await db.execute(
                select(SimulationJob).where(SimulationJob.id == job_id))).scalar_one()
            for k, v in updates.items():
                setattr(row, k, v)
            await db.commit()
    finally:
        await engine.dispose()


async def _read_row(job_id: uuid.UUID) -> SimulationJob:
    engine, Session = _sessions()
    try:
        async with Session() as db:
            return (await db.execute(
                select(SimulationJob).where(SimulationJob.id == job_id))).scalar_one()
    finally:
        await engine.dispose()


async def test_publish_recovery_heals_lapsed_fence_at_async_seam(job):
    from meshpipeline.application import execution_fence as _fence

    engine, Session = _sessions()
    try:
        claim = await _claim(job, Session, backend_execution_id="exec-heal")
        _drop_fence(str(job))
        with _fence.execution_ownership(claim.ownership, session_factory=Session):
            await _gated(str(job)).anote("healed hello", op_id="heal:1")
        assert _backlog(str(job)) == 1, "the healed publish never landed"
        assert _fence_pttl(str(job)) > 0, "the fence key was not reinstalled"
    finally:
        await engine.dispose()


async def test_healed_fence_ttl_tracks_renewed_lease(job):
    from meshpipeline.application import execution_fence as _fence

    engine, Session = _sessions()
    try:
        claim = await _claim(job, Session, backend_execution_id="exec-ttl")
        _drop_fence(str(job))
        with _fence.execution_ownership(claim.ownership, session_factory=Session):
            await _gated(str(job)).anote("hello", op_id="ttl:1")
        row = await _read_row(job)
        now = dt.datetime.now(dt.UTC)
        expires = row.lease_expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=dt.UTC)
        remaining_ms = (expires - now).total_seconds() * 1000
        pttl = _fence_pttl(str(job))
        assert 0 < pttl <= remaining_ms + 1500, (
            f"the mirror (pttl={pttl}ms) must never outlive the lease ({remaining_ms:.0f}ms)")
    finally:
        await engine.dispose()


async def test_an_expired_but_unclaimed_lease_is_renewed_by_recovery(job):
    # THE ORIGINAL KILL: heartbeats starved through a long stretch, the lease expired, the
    # mirror lapsed - but nobody took the job over. The first publish after the stretch used
    # to die StaleExecutionPublish with a healthy mesh mid-review; now it renews and lands.
    from meshpipeline.application import execution_fence as _fence

    engine, Session = _sessions()
    try:
        claim = await _claim(job, Session, backend_execution_id="exec-benchy")
        await _set_row(job, lease_expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=5))
        _drop_fence(str(job))
        with _fence.execution_ownership(claim.ownership, session_factory=Session):
            await _gated(str(job)).anote("back from the long render", op_id="benchy:1")
        assert _backlog(str(job)) == 1
        row = await _read_row(job)
        expires = row.lease_expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=dt.UTC)
        assert expires > dt.datetime.now(dt.UTC), "recovery renewed the unclaimed lease"
    finally:
        await engine.dispose()


async def test_recovery_refuses_past_the_pipeline_deadline(job):
    from meshpipeline.application import execution_fence as _fence

    engine, Session = _sessions()
    try:
        claim = await _claim(job, Session, backend_execution_id="exec-deadline")
        await _set_row(job,
                       pipeline_deadline_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=5))
        _drop_fence(str(job))
        with _fence.execution_ownership(claim.ownership, session_factory=Session):
            with pytest.raises(StaleExecutionPublish):
                await _gated(str(job)).anote("zombie hello", op_id="dead:1")
        assert _backlog(str(job)) == 0, "a past-deadline worker published durably"
    finally:
        await engine.dispose()


async def test_publish_heal_cannot_resurrect_a_takeover_revoked_fence(job):
    # A takeover writes under the row lock: generation bumped, outgoing fence revoked, all
    # before commit. A stale worker's recovery must BLOCK on that lock, then refuse against
    # the committed bump - the heal can never put the old fingerprint back.
    from meshpipeline.application import execution_fence as _fence

    engine, Session = _sessions()
    try:
        claim = await _claim(job, Session, backend_execution_id="exec-old")
        stale_own = claim.ownership
        _before = _backlog(str(job))

        async with Session() as takeover_db:
            row = (await takeover_db.execute(
                select(SimulationJob).where(SimulationJob.id == job)
                .with_for_update())).scalar_one()
            row.execution_generation = int(row.execution_generation) + 1
            row.active_worker_token = uuid.uuid4()
            _drop_fence(str(job))                 # the takeover's in-lock revoke
            await takeover_db.flush()

            async def _stale_publish():
                with _fence.execution_ownership(stale_own, session_factory=Session):
                    await _gated(str(job)).anote("stale hello", op_id="stale:1")

            task = asyncio.create_task(_stale_publish())
            await asyncio.sleep(1.0)
            # the recovery's FOR UPDATE is parked behind the takeover's uncommitted lock
            assert not task.done(), "recovery ran through an uncommitted takeover's row lock"
            await takeover_db.commit()

            with pytest.raises(StaleExecutionPublish):
                await asyncio.wait_for(task, timeout=30)
        assert _backlog(str(job)) == _before, "a superseded worker published durably"
        r = _client()
        try:
            val = r.get(fence_key_for(str(job)))
        finally:
            r.close()
        assert val is None, "the heal resurrected a revoked fence"
    finally:
        await engine.dispose()


async def test_a_predecessor_token_cannot_heal_after_same_generation_resume(job):
    # Same-execution resume keeps the generation but rotates the token. The predecessor's
    # ownership tuple must not be able to heal itself back in: its token no longer matches.
    from meshpipeline.application import execution_fence as _fence

    engine, Session = _sessions()
    try:
        first = await _claim(job, Session, backend_execution_id="exec-resume")
        old_own = first.ownership
        # a live lease refuses any second claim (active_lease_conflict) - the resume this test
        # is about happens after the first worker died and its lease aged out
        await _set_row(job, lease_expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=5))
        second = await _claim(job, Session, backend_execution_id="exec-resume")
        assert second.ownership.execution_generation == old_own.execution_generation
        assert second.ownership.worker_token != old_own.worker_token
        _drop_fence(str(job))
        with _fence.execution_ownership(old_own, session_factory=Session):
            with pytest.raises(StaleExecutionPublish):
                await _gated(str(job)).anote("predecessor hello", op_id="pred:1")
        assert _backlog(str(job)) == 0
    finally:
        await engine.dispose()
