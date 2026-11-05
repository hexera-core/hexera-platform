# Responsibility: Verify a retryable reconciliation failure waits its scheduled interval against real PostgreSQL.
# Boundaries: the repository's claim/resolve authority and the database clock; the sweep's own decisions are elsewhere.
from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.asyncio
if not os.getenv("DATABASE_URL"):
    pytest.skip("a real PostgreSQL endpoint is required for the schedule",
                allow_module_level=True)

from meshpipeline.persistence.models import ArtifactType, ReconciliationState  # noqa: E402
from meshpipeline.persistence.repositories.reconciliation_repository import (  # noqa: E402
    ReconciliationRepository,
)

OWNER = "recon-schedule-owner"


@pytest.fixture(autouse=True)
async def _only_this_suites_rows():
    # This suite reasons about which rows are CLAIMABLE, so it must not see another suite's
    # pending work. Everything it creates belongs to its own owner, removed before and after.
    from sqlalchemy import text

    from meshpipeline.persistence.session import get_db

    async def purge():
        async with get_db() as db:
            await db.execute(text("delete from artifact_reconciliations where owner_id = :o"),
                             {"o": OWNER})
            await db.execute(text("delete from simulation_jobs where owner_id = :o"), {"o": OWNER})
            await db.commit()

    await purge()
    yield
    await purge()


async def _job() -> uuid.UUID:
    from meshpipeline.persistence.repositories.job_repository import JobRepository
    from meshpipeline.persistence.session import get_db
    async with get_db() as db:
        job = await JobRepository().create(db, owner_id=OWNER)
        await db.commit()
        return job.id


async def _orphan(job_id: uuid.UUID, key: str) -> None:
    from meshpipeline.persistence.session import get_db
    async with get_db() as db:
        await ReconciliationRepository().record_orphan(
            db, owner_id=OWNER, job_id=job_id, delivery_attempt=0, logical_key="mesh_bundle",
            artifact_type=ArtifactType.mesh_bundle, object_key=key,
            object_checksum="etag", object_size=1)
        await db.commit()


async def _claim(limit: int = 50) -> list:
    from meshpipeline.persistence.session import get_db
    async with get_db() as db:
        rows = await ReconciliationRepository().claim_pending(db, limit=limit)
        return [(r.id, r.retry_count, r.state) for r in rows if r.owner_id == OWNER]


async def _resolve(rec_id, state, *, delay_s=None) -> None:
    from meshpipeline.persistence.session import get_db
    async with get_db() as db:
        await ReconciliationRepository().resolve(db, rec_id, state=state, detail="t",
                                                 retry_delay_s=delay_s)
        await db.commit()


async def _row(rec_id):
    from meshpipeline.persistence.models import ArtifactReconciliation
    from meshpipeline.persistence.session import get_db
    async with get_db() as db:
        return await db.get(ArtifactReconciliation, rec_id)


# 1. new work is immediately eligible

async def test_a_new_orphan_is_claimable_at_once():
    job = await _job()
    await _orphan(job, f"jobs/{job}/a.tar.gz")
    claimed = await _claim()
    assert len(claimed) == 1
    assert (await _row(claimed[0][0])).next_attempt_at is None, "new work carries no schedule"


# 2/3. a retryable failure schedules, and the row is not offered again before it is due

async def test_a_retryable_failure_schedules_and_is_not_reclaimable_before_it_is_due():
    job = await _job()
    await _orphan(job, f"jobs/{job}/b.tar.gz")
    (rec_id, _, _), = await _claim()

    await _resolve(rec_id, ReconciliationState.pending, delay_s=3600)
    row = await _row(rec_id)
    assert row.state is ReconciliationState.pending
    assert row.retry_count == 1
    assert row.next_attempt_at is not None, "a retryable failure must schedule its next attempt"

    assert await _claim() == [], "a scheduled row must not be offered again before it is due"


# 4. a due row becomes claimable again

async def test_a_due_row_is_claimable_again():
    job = await _job()
    await _orphan(job, f"jobs/{job}/c.tar.gz")
    (rec_id, _, _), = await _claim()
    # scheduled into the PAST through the same authority: the database's own clock decides
    await _resolve(rec_id, ReconciliationState.pending, delay_s=-5)
    again = await _claim()
    assert [r[0] for r in again] == [rec_id]
    assert again[0][1] == 1, "the attempt already spent is carried, not reset"


# 5. repeated failures reach the limit exactly

async def test_repeated_failures_reach_the_retry_limit_exactly():
    from meshpipeline.application.maintenance.reconcile import RECONCILE_MAX_RETRIES

    job = await _job()
    await _orphan(job, f"jobs/{job}/d.tar.gz")
    (rec_id, _, _), = await _claim()
    for attempt in range(1, RECONCILE_MAX_RETRIES):
        await _resolve(rec_id, ReconciliationState.pending, delay_s=-1)
        assert (await _row(rec_id)).retry_count == attempt
        assert [r[0] for r in await _claim()] == [rec_id], f"attempt {attempt} was not offered"
    # the sweep's own rule terminalizes at the limit; the schedule never grants a further one
    await _resolve(rec_id, ReconciliationState.abandoned)
    row = await _row(rec_id)
    assert row.retry_count == RECONCILE_MAX_RETRIES
    assert await _claim() == [], "an abandoned row must never be offered again"


# 6. success terminalizes

@pytest.mark.parametrize("terminal", [ReconciliationState.resolved_deleted,
                                      ReconciliationState.resolved_adopted,
                                      ReconciliationState.blocked_conflict])
async def test_a_terminal_row_is_never_claimed_again(terminal):
    job = await _job()
    await _orphan(job, f"jobs/{job}/{terminal.value}.tar.gz")
    (rec_id, _, _), = await _claim()
    await _resolve(rec_id, terminal)
    row = await _row(rec_id)
    assert row.state is terminal
    assert row.next_attempt_at is None, "a terminal row keeps no schedule to misread later"
    assert await _claim() == []


# 7. two concurrent claimers cannot own the same row

async def test_two_concurrent_claimers_cannot_own_the_same_row():
    from meshpipeline.persistence.session import get_db

    job = await _job()
    for i in range(4):
        await _orphan(job, f"jobs/{job}/conc-{i}.tar.gz")

    async with get_db() as a, get_db() as b:
        first = [r.id for r in await ReconciliationRepository().claim_pending(a, limit=4)
                 if r.owner_id == OWNER]
        # the second claimer runs while the first still holds its row locks
        second = [r.id for r in await ReconciliationRepository().claim_pending(b, limit=4)
                  if r.owner_id == OWNER]
        assert first, "the first claimer got nothing"
        assert not (set(first) & set(second)), (
            f"both claimers were handed the same row(s): {sorted(set(first) & set(second))}")


# 8. a rolled-back attempt does not consume the schedule

async def test_a_rolled_back_resolution_does_not_consume_an_attempt():
    from meshpipeline.persistence.session import get_db

    job = await _job()
    await _orphan(job, f"jobs/{job}/rollback.tar.gz")
    (rec_id, _, _), = await _claim()

    try:
        async with get_db() as db:
            await ReconciliationRepository().resolve(
                db, rec_id, state=ReconciliationState.pending, detail="x", retry_delay_s=3600)
            raise RuntimeError("the worker died before it committed")
    except RuntimeError:
        pass

    row = await _row(rec_id)
    assert row.retry_count == 0, "an uncommitted attempt was counted"
    assert row.next_attempt_at is None
    assert [r[0] for r in await _claim()] == [rec_id], "the row must still be claimable"


# 9. the schedule is durable, not process state

async def test_the_schedule_survives_a_new_engine():
    from meshpipeline.persistence.session import dispose_engine

    job = await _job()
    await _orphan(job, f"jobs/{job}/durable.tar.gz")
    (rec_id, _, _), = await _claim()
    await _resolve(rec_id, ReconciliationState.pending, delay_s=3600)

    await dispose_engine()          # the restart: every pooled connection is gone
    assert await _claim() == [], "a fresh process must observe the same durable schedule"
    row = await _row(rec_id)
    assert row.next_attempt_at is not None


# ordering among due rows is deterministic

async def test_due_rows_are_offered_in_a_deterministic_order():
    job = await _job()
    for i in range(3):
        await _orphan(job, f"jobs/{job}/order-{i}.tar.gz")
    first = [r[0] for r in await _claim()]
    second = [r[0] for r in await _claim()]
    assert first == second, "two identical claims saw different orders"
