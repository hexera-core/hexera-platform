# Responsibility: Verify status, final result and outbox commit atomically, and a pending row is delivered later.
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests import harness_provisioning as hp

import meshpipeline.settings.providers as provcfg
from meshpipeline.adapters._shared.redis_client import sync_client
from meshpipeline.adapters.event_stream.redis import JobPublisher
from meshpipeline.application import outbox_publisher as op
from meshpipeline.application import terminal_finalize as tfin
from meshpipeline.application.final_result import (
    TerminalStatus,
    build_final_result,
    render_message,
)
from meshpipeline.contracts import event_stream
from meshpipeline.events.channels import log_key_for
from meshpipeline.persistence.lease import ExecutionOwnership, LeaseRepository
from meshpipeline.persistence.models import JobStatus, SimulationJob
from meshpipeline.persistence.repositories.terminal_outbox_repository import (
    TerminalOutboxRepository,
    dedup_key_for,
)

lease = LeaseRepository()
outbox = TerminalOutboxRepository()

# Route the abstract publisher seam at the concrete Redis JobPublisher for the whole module.
event_stream.set_publisher_factory(JobPublisher)


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


def _redis_terminal_count(job_id) -> int:
    r = sync_client()
    raw = r.lrange(log_key_for(str(job_id)), 0, -1)
    import json
    return sum(1 for e in (raw or []) if json.loads(e).get("type") in ("closing", "terminal", "final"))


async def _running_job_with_owner(SessionLocal, *, generation=1):
    token = uuid.uuid4()
    async with SessionLocal() as s:
        j = SimulationJob(owner_id="o", status=JobStatus.running,
                          execution_generation=generation, active_worker_token=token)
        s.add(j)
        await s.commit()
        jid = j.id
    return jid, ExecutionOwnership(job_id=jid, execution_generation=generation, worker_token=token,
                                   backend="celery", pipeline_deadline_at=None)


def _success_result(job_id):
    fr = build_final_result(
        job_id=str(job_id), owner_id="o", status=TerminalStatus.succeeded, engine="cfmesh",
        purpose="external_cfd", dimensionality="3d", approved_snapshot_id="snap",
        executor_success=True, reviewer_verdict="PASS", failed_gate="", api_failure="",
        attempts=1, attempts_max=3, required_ready=True, delivered_types=["mesh_bundle"],
        optional_warnings=[])
    return fr.to_dict(), render_message(fr)


async def _finalize(SessionLocal, ownership, *, status=JobStatus.succeeded, failed_reason=None):
    fr_dict, closing = _success_result(ownership.job_id)
    async with SessionLocal() as db:
        outcome = await tfin.finalize_terminal_atomic(
            db, ownership=ownership, intended_status=status, failed_reason=failed_reason,
            final_result_dict=fr_dict, closing_message=closing)
        await db.commit()
        return outcome


# atomic finalize: status + final_result + outbox commit together
async def test_finalize_commits_status_final_result_and_outbox_atomically(SessionLocal):
    jid, own = await _running_job_with_owner(SessionLocal)
    outcome = await _finalize(SessionLocal, own)
    assert outcome.fenced is False and outcome.enqueued is True
    async with SessionLocal() as s:
        row = await s.get(SimulationJob, jid)
        assert row.status == JobStatus.succeeded
        assert row.final_result is not None and row.final_result["status"] == "succeeded"
        obx = await outbox.get_by_job(s, jid)
        assert len(obx) == 1 and obx[0].dedup_key == dedup_key_for(jid)
        assert obx[0].published_at is None                 # enqueued, not yet delivered


# a fenced (superseded) worker writes NOTHING
async def test_fenced_worker_writes_nothing(SessionLocal):
    jid, own_gen1 = await _running_job_with_owner(SessionLocal, generation=1)
    # a newer generation takes over the row
    async with SessionLocal() as s:
        row = await s.get(SimulationJob, jid)
        row.execution_generation = 2
        row.active_worker_token = uuid.uuid4()
        await s.commit()
    outcome = await _finalize(SessionLocal, own_gen1)      # stale gen-1 owner tries to finalize
    assert outcome.fenced is True and outcome.enqueued is False
    async with SessionLocal() as s:
        row = await s.get(SimulationJob, jid)
        assert row.status == JobStatus.running             # NOT finalized by the stale worker
        assert row.final_result is None
        assert await outbox.get_by_job(s, jid) == []       # no terminal event enqueued


# ...and the worker that SUPERSEDED it is still the one that may finalize.
# The half the refusal above does not prove: fencing must protect the current owner's authority,
# not merely block the stale one. A fence that also broke the live worker would turn a takeover
# into a job nobody can finish. This is the assertion the mutation control is aimed at - remove
# the row-lock fence inside finalize_terminal_atomic and the stale worker terminalizes the job,
# which is visible here as a job the CURRENT owner then cannot finalize.
async def test_the_current_owner_remains_authoritative_after_fencing_a_stale_worker(SessionLocal):
    jid, own_gen1 = await _running_job_with_owner(SessionLocal, generation=1)
    new_token = uuid.uuid4()
    async with SessionLocal() as s:
        row = await s.get(SimulationJob, jid)
        row.execution_generation = 2
        row.active_worker_token = new_token
        await s.commit()
    own_gen2 = ExecutionOwnership(job_id=jid, execution_generation=2, worker_token=new_token,
                                  backend="celery", pipeline_deadline_at=None)

    stale = await _finalize(SessionLocal, own_gen1)
    assert stale.fenced is True, "the superseded worker was allowed to finalize"
    async with SessionLocal() as s:
        assert (await s.get(SimulationJob, jid)).status == JobStatus.running

    current = await _finalize(SessionLocal, own_gen2)
    assert current.fenced is False and current.enqueued is True, (
        "the CURRENT owner could not finalize after a stale worker was fenced")
    async with SessionLocal() as s:
        row = await s.get(SimulationJob, jid)
        assert row.status == JobStatus.succeeded
        assert row.final_result is not None
        rows = await outbox.get_by_job(s, jid)
        assert len(rows) == 1, f"expected exactly one terminal event, got {len(rows)}"


# dedup: two finalizes for the same job → exactly one outbox row
async def test_double_finalize_same_owner_enqueues_once(SessionLocal):
    jid, own = await _running_job_with_owner(SessionLocal)
    o1 = await _finalize(SessionLocal, own)
    o2 = await _finalize(SessionLocal, own)                # idempotent re-finalize (redelivery)
    assert o1.enqueued is True and o2.enqueued is False    # second did not insert a 2nd row
    async with SessionLocal() as s:
        assert len(await outbox.get_by_job(s, jid)) == 1


# publisher delivers a committed row to the durable stream; idempotent on re-sweep
async def test_publisher_delivers_and_is_idempotent(SessionLocal):
    jid, own = await _running_job_with_owner(SessionLocal)
    await _finalize(SessionLocal, own)
    sync_client().delete(log_key_for(str(jid)))            # clean slate for the assertion
    stats = await op.publish_pending(SessionLocal)
    assert stats.published == 1
    assert _redis_terminal_count(jid) == 1                 # terminal event landed in the durable log
    async with SessionLocal() as s:
        assert (await outbox.get_by_job(s, jid))[0].published_at is not None
    stats2 = await op.publish_pending(SessionLocal)        # re-sweep
    assert stats2.published == 0                           # already delivered - not re-marked
    assert _redis_terminal_count(jid) == 1                 # and NOT re-emitted


# crash recovery: a committed row whose worker died is delivered by the sweep
async def test_crash_recovery_sweep_delivers_orphan(SessionLocal):
    jid, own = await _running_job_with_owner(SessionLocal)
    await _finalize(SessionLocal, own)                     # committed, but the "worker" never published
    sync_client().delete(log_key_for(str(jid)))
    delivered = await op.publish_pending(SessionLocal)     # a DIFFERENT process runs the sweep
    assert delivered.published == 1
    assert _redis_terminal_count(jid) == 1


# a transport failure leaves the row pending; a later good sweep delivers it
async def test_publish_failure_leaves_row_pending_then_recovers(SessionLocal):
    jid, own = await _running_job_with_owner(SessionLocal)
    await _finalize(SessionLocal, own)
    sync_client().delete(log_key_for(str(jid)))

    class _Boom:
        def __init__(self, job_id, agent=None): ...
        def publish_terminal(self, text, event_id=""): raise RuntimeError("redis down")

    event_stream.set_publisher_factory(_Boom)
    try:
        bad = await op.publish_pending(SessionLocal)
        assert bad.published == 0 and bad.failed == 1
        async with SessionLocal() as s:
            row = (await outbox.get_by_job(s, jid))[0]
            assert row.published_at is None and row.last_error == "RuntimeError"
            assert row.publish_attempts == 1
    finally:
        event_stream.set_publisher_factory(JobPublisher)
    good = await op.publish_pending(SessionLocal)          # transport restored
    assert good.published == 1
    assert _redis_terminal_count(jid) == 1


# a locked row must not block OTHER jobs' terminal events (SKIP LOCKED, not plain FOR UPDATE)
async def test_a_locked_row_does_not_block_other_jobs_publication(SessionLocal):
    jid_a, own_a = await _running_job_with_owner(SessionLocal)
    await _finalize(SessionLocal, own_a)
    jid_b, own_b = await _running_job_with_owner(SessionLocal)
    await _finalize(SessionLocal, own_b)
    for j in (jid_a, jid_b):
        sync_client().delete(log_key_for(str(j)))

    async with SessionLocal() as holder:                 # hold job A's row lock, uncommitted
        held = await outbox.claim_pending(holder, limit=10, job_id=jid_a)
        assert len(held) == 1
        # a second publisher must still deliver job B without waiting on the held lock
        stats = await asyncio.wait_for(op.publish_pending(SessionLocal, limit=10), timeout=5)
        assert stats.published == 1                      # B delivered; A skipped, not blocked
        assert _redis_terminal_count(jid_b) == 1
        assert _redis_terminal_count(jid_a) == 0
        await holder.rollback()
    # once the lock is released the skipped row is picked up by the next sweep
    again = await op.publish_pending(SessionLocal, limit=10)
    assert again.published == 1 and _redis_terminal_count(jid_a) == 1


# concurrent sweeps never double-deliver (FOR UPDATE SKIP LOCKED)
async def test_concurrent_sweeps_no_double_delivery(SessionLocal):
    jids = []
    for _ in range(6):
        jid, own = await _running_job_with_owner(SessionLocal)
        await _finalize(SessionLocal, own)
        sync_client().delete(log_key_for(str(jid)))
        jids.append(jid)
    a, b = await asyncio.gather(op.publish_pending(SessionLocal, limit=10),
                                op.publish_pending(SessionLocal, limit=10))
    assert a.published + b.published == 6                  # each row delivered exactly once total
    for jid in jids:
        assert _redis_terminal_count(jid) == 1


# the post-publish / pre-acknowledgement crash
async def test_publisher_dies_after_redis_accepts_but_before_db_ack(SessionLocal):
    jid, own = await _running_job_with_owner(SessionLocal)
    await _finalize(SessionLocal, own)
    sync_client().delete(log_key_for(str(jid)))

    real = JobPublisher

    class _DiesAfterPublish:
        def __init__(self, job_id, agent=None):
            self._inner = real(job_id, agent or "")
        def publish_terminal(self, text, event_id=""):
            self._inner.publish_terminal(text, event_id)      # Redis ACCEPTS the event ...
            raise RuntimeError("worker died before recording the acknowledgement")

    event_stream.set_publisher_factory(_DiesAfterPublish)
    try:
        crashed = await op.publish_pending(SessionLocal)
        assert crashed.published == 0 and crashed.failed == 1
    finally:
        event_stream.set_publisher_factory(JobPublisher)

    # the event IS on the stream, but the row is still pending - exactly the ambiguous state
    assert _redis_terminal_count(jid) == 1
    async with SessionLocal() as s:
        assert (await outbox.get_by_job(s, jid))[0].published_at is None

    # a recovering publisher redelivers it, and now records the acknowledgement
    recovered = await op.publish_pending(SessionLocal)
    assert recovered.published == 1
    async with SessionLocal() as s:
        assert (await outbox.get_by_job(s, jid))[0].published_at is not None

    # The outbox redelivered, but the STREAM refused the duplicate: the closing carries the same
    # dedup key the outbox row is unique on, so the recovering publisher's second attempt is
    # recognised as the same announcement rather than a second ending. The user sees one closing.
    import json
    raw = sync_client().lrange(log_key_for(str(jid)), 0, -1)
    closings = [json.loads(e) for e in raw if json.loads(e).get("type") == "closing"]
    assert len(closings) == 1, [c["seq"] for c in closings]
    assert closings[0]["event_id"] == dedup_key_for(jid)       # the identity the outbox owns

    # and the suppression is the CLAIM, not a coincidence: dropping the claim set lets a further
    # redelivery through, which is what the guard is preventing on the path above.
    from meshpipeline.events.channels import opkey_set_for
    sync_client().delete(opkey_set_for(str(jid)))
    event_stream.publisher(str(jid)).closing(closings[0]["text"], dedup_key_for(jid))
    assert len([e for e in sync_client().lrange(log_key_for(str(jid)), 0, -1)
                if json.loads(e).get("type") == "closing"]) == 2


async def test_the_published_event_matches_the_persisted_final_result(SessionLocal):
    jid, own = await _running_job_with_owner(SessionLocal)
    await _finalize(SessionLocal, own)
    sync_client().delete(log_key_for(str(jid)))
    await op.publish_pending(SessionLocal)

    import json
    raw = sync_client().lrange(log_key_for(str(jid)), 0, -1)
    closing = next(json.loads(e) for e in raw if json.loads(e).get("type") == "closing")
    async with SessionLocal() as s:
        row = await s.get(SimulationJob, jid)
        obx = (await outbox.get_by_job(s, jid))[0]
    assert closing["text"] == obx.event_payload["closing_message"]
    assert obx.event_payload["final_result"] == row.final_result   # the DURABLE verdict
    assert obx.terminal_status == row.status.value


async def test_a_stale_generation_cannot_publish_a_terminal_event(SessionLocal):
    jid, own_gen1 = await _running_job_with_owner(SessionLocal, generation=1)
    async with SessionLocal() as s:
        row = await s.get(SimulationJob, jid)
        row.execution_generation = 2
        row.active_worker_token = uuid.uuid4()
        await s.commit()
    sync_client().delete(log_key_for(str(jid)))
    outcome = await _finalize(SessionLocal, own_gen1)
    assert outcome.fenced is True
    assert (await op.publish_pending(SessionLocal)).published == 0
    assert _redis_terminal_count(jid) == 0
