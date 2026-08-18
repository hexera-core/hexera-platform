# Responsibility: Verify each job-status race resolves once, and a terminal state is never overwritten.
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests import harness_provisioning as hp

import meshpipeline.settings.policy as polcfg
import meshpipeline.settings.providers as provcfg
from meshpipeline.application.job_service import JobService
from meshpipeline.persistence.job_state import TransitionResult
from meshpipeline.persistence.models import JobStatus, SimulationJob
from meshpipeline.persistence.repositories.job_repository import JobRepository

repo = JobRepository()
svc = JobService()


@pytest.fixture()
async def SessionLocal():
    connect_args = {"ssl": True} if provcfg.DB_SSL_REQUIRED else {}
    try:
        engine = create_async_engine(provcfg.POSTGRES_DSN, connect_args=connect_args, pool_size=6)
        # Alembic is the only thing that creates this schema - see
        # tests/harness_provisioning.py. `create_all` built tables no migration
        # had produced, so a suite could pass against a schema production never has.
        await hp.reset_schema(provcfg.POSTGRES_DSN)
        await hp.truncate_tables(provcfg.POSTGRES_DSN, "simulation_jobs")
    except Exception as exc:  # noqa: BLE001
        pytest.fail("PostgreSQL not reachable for the transition/quota concurrency test - it must "
                    f"be PROVISIONED, not skipped ({type(exc).__name__}: {exc})")
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed(SessionLocal, status: JobStatus, owner: str = "owner-1") -> uuid.UUID:
    async with SessionLocal() as s:
        job = SimulationJob(owner_id=owner, status=status)
        s.add(job)
        await s.commit()
        return job.id


async def _status(SessionLocal, job_id) -> JobStatus:
    async with SessionLocal() as s:
        return (await repo.get_internal(s, job_id)).status


async def _race(SessionLocal, job_id, op_a, op_b):
    async def _run(op):
        async with SessionLocal() as s:
            r = await op(s, job_id)
            await s.commit()
            return r
    return await asyncio.gather(_run(op_a), _run(op_b))


_to = lambda target, allow=None: (lambda s, jid: repo.transition(s, jid, target, allow=allow))  # noqa: E731


def _exactly_one_applied(results):
    return sum(1 for r in results if r == TransitionResult.applied) == 1


# the eight required races

async def test_race1_succeeded_vs_failed(SessionLocal):
    jid = await _seed(SessionLocal, JobStatus.running)
    res = await _race(SessionLocal, jid, _to(JobStatus.succeeded), _to(JobStatus.failed))
    assert _exactly_one_applied(res), res
    assert TransitionResult.rejected_current_state in res
    assert (await _status(SessionLocal, jid)) in (JobStatus.succeeded, JobStatus.failed)


async def test_race2_failed_vs_succeeded(SessionLocal):
    jid = await _seed(SessionLocal, JobStatus.running)
    res = await _race(SessionLocal, jid, _to(JobStatus.failed), _to(JobStatus.succeeded))
    assert _exactly_one_applied(res) and TransitionResult.rejected_current_state in res


async def test_race3_completion_vs_redelivery_reset(SessionLocal):
    jid = await _seed(SessionLocal, JobStatus.succeeded)
    async with SessionLocal() as s:
        reset = await repo.transition(s, jid, JobStatus.pending)  # legal only from running
        await s.commit()
    assert reset == TransitionResult.rejected_current_state
    assert (await _status(SessionLocal, jid)) == JobStatus.succeeded


async def test_race4_completion_vs_launch_failure(SessionLocal):
    jid = await _seed(SessionLocal, JobStatus.running)

    async def _complete(s, j):
        return await repo.transition(s, j, JobStatus.succeeded)

    async def _launch_fail(s, j):
        await repo.mark_launch_failed(s, j, "launcher exploded")  # commits internally, CAS-guarded
        return "launch_failed"

    await _race(SessionLocal, jid, _complete, _launch_fail)
    final = await _status(SessionLocal, jid)
    assert final in (JobStatus.succeeded, JobStatus.failed)
    # stable: re-reading does not change it
    assert (await _status(SessionLocal, jid)) == final


async def test_race5_two_workers_start_from_pending(SessionLocal):
    jid = await _seed(SessionLocal, JobStatus.pending)
    res = await _race(SessionLocal, jid, _to(JobStatus.running), _to(JobStatus.running))
    assert _exactly_one_applied(res)
    assert TransitionResult.already_at_target in res     # the loser sees the job already running
    assert (await _status(SessionLocal, jid)) == JobStatus.running


async def test_race6_duplicate_succeeded_is_idempotent(SessionLocal):
    jid = await _seed(SessionLocal, JobStatus.running)
    async with SessionLocal() as s:
        assert await repo.transition(s, jid, JobStatus.succeeded) == TransitionResult.applied
        await s.commit()
    async with SessionLocal() as s:
        assert await repo.transition(s, jid, JobStatus.succeeded) == TransitionResult.already_at_target
        await s.commit()
    assert (await _status(SessionLocal, jid)) == JobStatus.succeeded


async def test_race7_pending_review_to_succeeded_vs_failed(SessionLocal):
    jid = await _seed(SessionLocal, JobStatus.pending_review)
    res = await _race(SessionLocal, jid, _to(JobStatus.succeeded), _to(JobStatus.failed))
    assert _exactly_one_applied(res)
    assert (await _status(SessionLocal, jid)) in (JobStatus.succeeded, JobStatus.failed)


async def test_race8_reaper_vs_completion(SessionLocal):
    jid = await _seed(SessionLocal, JobStatus.running)
    reaper = _to(JobStatus.failed, allow={JobStatus.running, JobStatus.pending, JobStatus.queued})
    res = await _race(SessionLocal, jid, _to(JobStatus.succeeded), reaper)
    assert _exactly_one_applied(res)
    assert (await _status(SessionLocal, jid)) in (JobStatus.succeeded, JobStatus.failed)


async def test_rollback_leaves_no_partial_state(SessionLocal):
    jid = await _seed(SessionLocal, JobStatus.pending)
    async with SessionLocal() as s:
        await repo.transition(s, jid, JobStatus.running)
        await s.rollback()
    assert (await _status(SessionLocal, jid)) == JobStatus.pending   # transition did not persist


async def test_mutation_removing_the_status_predicate_would_break_race1(SessionLocal, monkeypatch):
    jid = await _seed(SessionLocal, JobStatus.succeeded)
    from sqlalchemy import update
    async with SessionLocal() as s:
        # the mutation: last-writer-wins, no status guard
        await s.execute(update(SimulationJob).where(SimulationJob.id == jid).values(status=JobStatus.failed))
        await s.commit()
    assert (await _status(SessionLocal, jid)) == JobStatus.failed  # terminal WAS overwritten -> the CAS matters


# the follow-up-SELECT result contract when the CAS loses (READ COMMITTED)
# transition() runs a follow-up SELECT only when its CAS matched zero rows, to DESCRIBE why. These
# prove that, whatever a concurrent transaction committed first, the durable state stays correct AND
# the returned code truthfully names the observed committed state. Each is deterministic: the other
# transaction commits, THEN transition observes it.

async def test_cas_loss_to_a_concurrent_delete_returns_not_found(SessionLocal):
    jid = await _seed(SessionLocal, JobStatus.running)
    async with SessionLocal() as s:                       # another txn deletes the row
        await s.execute(delete(SimulationJob).where(SimulationJob.id == jid))
        await s.commit()
    async with SessionLocal() as s:
        r = await repo.transition(s, jid, JobStatus.succeeded)
        await s.commit()
    assert r == TransitionResult.not_found
    async with SessionLocal() as s:
        assert await repo.get_internal(s, jid) is None             # durable: no row


async def test_cas_loss_because_row_reached_the_requested_target_is_already_at_target(SessionLocal):
    jid = await _seed(SessionLocal, JobStatus.running)
    async with SessionLocal() as s:                       # another txn moves it TO the target
        assert await repo.transition(s, jid, JobStatus.succeeded) == TransitionResult.applied
        await s.commit()
    async with SessionLocal() as s:
        r = await repo.transition(s, jid, JobStatus.succeeded)
        await s.commit()
    assert r == TransitionResult.already_at_target
    assert (await _status(SessionLocal, jid)) == JobStatus.succeeded


async def test_cas_loss_to_a_different_nonterminal_state_is_rejected(SessionLocal):
    jid = await _seed(SessionLocal, JobStatus.running)
    async with SessionLocal() as s:                       # another txn moves it to a DIFFERENT nonterminal
        assert await repo.transition(s, jid, JobStatus.pending) == TransitionResult.applied
        await s.commit()
    async with SessionLocal() as s:
        r = await repo.transition(s, jid, JobStatus.succeeded)   # pending is not a legal source
        await s.commit()
    assert r == TransitionResult.rejected_current_state
    assert (await _status(SessionLocal, jid)) == JobStatus.pending


async def test_cas_loss_to_a_terminal_state_is_rejected_and_never_overwrites(SessionLocal):
    jid = await _seed(SessionLocal, JobStatus.running)
    async with SessionLocal() as s:                       # another txn reaches a TERMINAL state first
        assert await repo.transition(s, jid, JobStatus.failed) == TransitionResult.applied
        await s.commit()
    async with SessionLocal() as s:
        r = await repo.transition(s, jid, JobStatus.succeeded)
        await s.commit()
    assert r == TransitionResult.rejected_current_state
    assert (await _status(SessionLocal, jid)) == JobStatus.failed   # terminal NOT overwritten


# atomic per-owner quota (Fix 4)

async def test_concurrent_submissions_for_one_owner_cannot_exceed_the_limit(SessionLocal):
    owner = "quota-owner"
    # seed the owner at the limit minus one active job
    for _ in range(polcfg.MAX_JOBS_PER_OWNER - 1):
        await _seed(SessionLocal, JobStatus.running, owner=owner)

    async def _submit():
        async with SessionLocal() as s:
            try:
                await svc.check_quotas(s, owner)
                # Hold the transaction OPEN after the count so BOTH submitters have passed their
                # count before EITHER inserts+commits - the exact interleaving the advisory lock
                # must survive. Without the lock both would count 4 here and both create (the
                # mutation this kills); with it, the second blocks in check_quotas until the first
                # commits and then counts 5. The sleep only forces the overlap; it is not the fix.
                await asyncio.sleep(0.4)
                await repo.create(s, owner)
                await s.commit()
                return "created"
            except ValueError:
                await s.rollback()
                return "quota"

    results = await asyncio.gather(_submit(), _submit())
    assert results.count("created") == 1, f"quota race let both through: {results}"
    assert results.count("quota") == 1
    async with SessionLocal() as s:
        assert await repo.count_active_for_owner(s, owner) == polcfg.MAX_JOBS_PER_OWNER


async def test_separate_owners_do_not_serialize_each_other(SessionLocal):
    async def _submit(owner):
        async with SessionLocal() as s:
            await svc.check_quotas(s, owner)
            await repo.create(s, owner)
            await s.commit()
            return "created"
    results = await asyncio.gather(_submit("owner-A"), _submit("owner-B"))
    assert results == ["created", "created"]
