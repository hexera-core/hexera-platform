# Responsibility: Verify every successful execution claim advances a job-local monotonic epoch, and nothing else does.
# Boundaries: claim ordering only - the epoch never enters a thread id, a generation or an event identity.
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests import harness_provisioning as hp

import meshpipeline.settings.providers as provcfg
from meshpipeline.persistence.lease import ClaimResult, ExecutionOwnership, LeaseRepository
from meshpipeline.persistence.models import JobStatus, SimulationJob

pytestmark = pytest.mark.asyncio
if not os.getenv("DATABASE_URL"):
    pytest.skip("a real PostgreSQL endpoint is required", allow_module_level=True)
lease = LeaseRepository()


@pytest.fixture()
async def SessionLocal():
    ca = {"ssl": True} if provcfg.DB_SSL_REQUIRED else {}
    engine = create_async_engine(provcfg.POSTGRES_DSN, connect_args=ca, pool_size=8)
    await hp.reset_schema(provcfg.POSTGRES_DSN)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


async def _job(SessionLocal):
    async with SessionLocal() as s:
        j = SimulationJob(owner_id="epoch", status=JobStatus.pending)
        s.add(j); await s.commit(); return j.id


async def _claim(SessionLocal, jid, exec_id, token=None):
    async with SessionLocal() as s:
        r, o = await lease.claim_execution(s, jid, worker_token=token or uuid.uuid4(),
                                           backend="celery", backend_execution_id=exec_id)
        await s.commit()
    return r, o


async def _row(SessionLocal, jid) -> dict:
    async with SessionLocal() as s:
        r = (await s.execute(text("select execution_generation, execution_claim_epoch, "
                                  "active_worker_token::text from simulation_jobs where id=:j"),
                             {"j": jid})).first()
    return {"gen": r[0], "epoch": r[1], "token": r[2]}


async def _expire(SessionLocal, jid):
    async with SessionLocal() as s:
        row = await s.get(SimulationJob, jid)
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await s.commit()


async def test_the_claim_matrix_orders_every_successful_claim(SessionLocal):
    jid = await _job(SessionLocal)

    r1, o1 = await _claim(SessionLocal, jid, "exec-A")
    s1 = await _row(SessionLocal, jid)
    assert (r1, s1["gen"], s1["epoch"]) == (ClaimResult.acquired_new_generation, 1, 1), s1
    assert o1.claim_epoch == 1

    r2, o2 = await _claim(SessionLocal, jid, "exec-A")          # lease still live
    s2 = await _row(SessionLocal, jid)
    assert r2 == ClaimResult.active_lease_conflict and o2 is None
    assert s2 == s1, "a refused claim moved the row"

    await _expire(SessionLocal, jid)
    r3, o3 = await _claim(SessionLocal, jid, "exec-A")          # same execution, continuation
    s3 = await _row(SessionLocal, jid)
    assert r3 == ClaimResult.resumed_same_generation
    assert s3["gen"] == 1, "a continuation changed the generation"
    assert s3["epoch"] == 2 and o3.claim_epoch == 2
    assert s3["token"] != s1["token"], "the token must still rotate on continuation"

    async with SessionLocal() as s:                              # heartbeat must not advance it
        await lease.heartbeat(s, o3); await s.commit()
    s4 = await _row(SessionLocal, jid)
    assert (s4["gen"], s4["epoch"], s4["token"]) == (1, 2, s3["token"]), s4

    await _expire(SessionLocal, jid)
    r5, o5 = await _claim(SessionLocal, jid, "exec-B")          # different execution, takeover
    s5 = await _row(SessionLocal, jid)
    assert r5 == ClaimResult.acquired_new_generation
    assert (s5["gen"], s5["epoch"]) == (2, 3) and o5.claim_epoch == 3
    assert s5["token"] not in (s1["token"], s3["token"])

    seen = [s1["epoch"], s2["epoch"], s3["epoch"], s4["epoch"], s5["epoch"]]
    assert seen == [1, 1, 2, 2, 3], seen
    assert all(b >= a for a, b in zip(seen, seen[1:])), f"the epoch decreased: {seen}"


async def test_only_one_claimer_wins_a_race_and_the_epoch_moves_once(SessionLocal):
    import asyncio
    jid = await _job(SessionLocal)
    await _claim(SessionLocal, jid, "exec-A")
    await _expire(SessionLocal, jid)
    before = await _row(SessionLocal, jid)

    results = await asyncio.gather(_claim(SessionLocal, jid, "exec-X"),
                                   _claim(SessionLocal, jid, "exec-Y"),
                                   return_exceptions=True)
    ok = [r for r in results if not isinstance(r, Exception) and r[1] is not None]
    after = await _row(SessionLocal, jid)
    assert len(ok) == 1, f"two claimers both won: {results}"
    assert after["epoch"] == before["epoch"] + 1, (before, after)


async def test_a_rolled_back_claim_leaves_the_epoch_unchanged(SessionLocal):
    jid = await _job(SessionLocal)
    await _claim(SessionLocal, jid, "exec-A")
    await _expire(SessionLocal, jid)
    before = await _row(SessionLocal, jid)
    async with SessionLocal() as s:
        await lease.claim_execution(s, jid, worker_token=uuid.uuid4(), backend="celery",
                                    backend_execution_id="exec-Z")
        await s.rollback()
    assert await _row(SessionLocal, jid) == before, "a rolled-back claim committed an increment"


#: The identity surfaces this contract covers. Each takes an ExecutionOwnership and returns the
#: identity production derives from it, built the way production builds it.
def _thread_identity(own) -> str:
    from meshpipeline.contracts.pipeline_state import STATE_SCHEMA_VERSION
    return f"{own.job_id}:s{STATE_SCHEMA_VERSION}:g{own.execution_generation}"


IDENTITY_SURFACES = {"thread_id": _thread_identity}


def _ownership(job_id, generation, epoch, token=None):
    return ExecutionOwnership(job_id=job_id, execution_generation=generation,
                              worker_token=token or uuid.uuid4(), backend="celery",
                              pipeline_deadline_at=None, claim_epoch=epoch)


@pytest.mark.parametrize("surface", sorted(IDENTITY_SURFACES))
def test_the_claim_epoch_changes_no_identity(surface):
    # Two ownerships alike in everything an identity may use, differing ONLY in claim_epoch. The
    # identities must be equal - compared as whole values, never searched for digits.
    derive = IDENTITY_SURFACES[surface]
    job_id, token = uuid.uuid4(), uuid.uuid4()
    low = _ownership(job_id, 7, 1, token)
    high = _ownership(job_id, 7, 99, token)
    assert low.claim_epoch != high.claim_epoch, "the two inputs do not differ in claim epoch"
    assert derive(low) == derive(high), (
        f"{surface} changed when only claim_epoch changed: {derive(low)!r} vs {derive(high)!r}")


@pytest.mark.parametrize("surface", sorted(IDENTITY_SURFACES))
def test_the_fields_that_belong_to_an_identity_still_change_it(surface):
    # The invariance above must not be satisfied by an identity that ignores everything.
    derive = IDENTITY_SURFACES[surface]
    base = _ownership(uuid.uuid4(), 7, 1)
    assert derive(base) != derive(_ownership(uuid.uuid4(), 7, 1)), \
        f"{surface} ignores the job it belongs to"
    assert derive(base) != derive(_ownership(base.job_id, 8, 1)), \
        f"{surface} ignores the execution generation"


@pytest.mark.parametrize("surface", sorted(IDENTITY_SURFACES))
def test_a_job_whose_text_contains_the_epoch_digits_is_still_invariant(surface):
    # The digits of a job UUID are data inside the job identity, not an epoch field. A fixed UUID
    # containing "99" with claim_epoch 99 is the case a substring search got wrong.
    derive = IDENTITY_SURFACES[surface]
    job_id = uuid.UUID("99999999-9999-4999-8999-999999999999")
    assert "99" in str(job_id)
    token = uuid.uuid4()
    assert derive(_ownership(job_id, 7, 99, token)) == derive(_ownership(job_id, 7, 1, token))


def test_the_claim_epoch_remains_a_non_negative_integer():
    own = _ownership(uuid.uuid4(), 7, 99)
    assert isinstance(own.claim_epoch, int) and own.claim_epoch >= 0
