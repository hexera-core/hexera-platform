# Responsibility: Verify every execution seam admits the current owner and refuses a superseded one, failing closed.
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests import harness_provisioning as hp

import meshpipeline.settings.providers as provcfg
from meshpipeline.application import execution_fence as fence
from meshpipeline.persistence.lease import ClaimResult, LeaseRepository
from meshpipeline.persistence.models import JobStatus, SimulationJob

lease = LeaseRepository()

# the seams under test are the COMPOSED ones, so the runtime factories must be bound
from datetime import UTC

from meshpipeline.runtime.composition import install_adapters  # noqa: E402

install_adapters()


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


async def _claimed_job(SessionLocal):
    # THE PRODUCTION CLAIM. Claiming the lease directly leaves Redis with no fingerprint for the
    # new generation, so the very publications this suite is about are refused at the fence with
    # nothing wrong with the claim. `claim_delivery` is what a worker really runs: it commits the
    # generation and then installs the mirror.
    from tests.integration import execution_ownership_support as ownership

    async with SessionLocal() as s:
        job = SimulationJob(owner_id="o", status=JobStatus.pending)
        s.add(job)
        await s.commit()
        jid = job.id
    own = await ownership.claim(SessionLocal, jid)
    assert own.execution_generation >= 1
    return jid, own


async def _take_over(SessionLocal, jid):
    async with SessionLocal() as s:
        row = await s.get(SimulationJob, jid)
        row.execution_generation = (row.execution_generation or 0) + 1
        row.active_worker_token = uuid.uuid4()
        await s.commit()


# the current owner is NOT fenced
async def test_the_current_owner_passes_every_seam(SessionLocal):
    _, own = await _claimed_job(SessionLocal)
    with fence.execution_ownership(own):
        for seam in ("graph node node_builder", "builder tool run_mesh",
                     "accept native run_mesh output", "graph node node_reviewer"):
            await fence.assert_current_owner(seam, session_factory=SessionLocal)
        assert await fence.is_current_owner(session_factory=SessionLocal) is True


# a superseded worker is refused at every seam
@pytest.mark.parametrize("seam", [
    "graph node node_builder",
    "graph node node_executor",
    "graph node node_reviewer",
    "builder tool run_mesh",
    "builder tool write_file",
    "accept native run_mesh output",
    "planner model call",
])
async def test_a_superseded_worker_is_refused_at_each_seam(SessionLocal, seam):
    jid, own = await _claimed_job(SessionLocal)
    await _take_over(SessionLocal, jid)
    with fence.execution_ownership(own):
        with pytest.raises(fence.StaleWorkerFenced) as ei:
            await fence.assert_current_owner(seam, session_factory=SessionLocal)
    assert ei.value.where == seam
    assert str(own.worker_token) not in str(ei.value)      # the raw token never appears in the error


async def test_fence_rejection_names_the_generation_not_the_token(SessionLocal):
    jid, own = await _claimed_job(SessionLocal)
    await _take_over(SessionLocal, jid)
    with fence.execution_ownership(own):
        with pytest.raises(fence.StaleWorkerFenced) as ei:
            await fence.assert_current_owner("graph node node_builder", session_factory=SessionLocal)
    msg = str(ei.value)
    assert own.token_hash() in msg and str(own.worker_token) not in msg
    assert ei.value.execution_generation == own.execution_generation


# a terminal job fences everything (nothing more may be done to it)
async def test_a_terminal_job_fences_further_work(SessionLocal):
    jid, own = await _claimed_job(SessionLocal)
    async with SessionLocal() as s:
        row = await s.get(SimulationJob, jid)
        row.status = JobStatus.succeeded
        await s.commit()
    with fence.execution_ownership(own):
        with pytest.raises(fence.StaleWorkerFenced):
            await fence.assert_current_owner("builder tool run_mesh", session_factory=SessionLocal)


# unbound ownership is a documented no-op (hermetic tests / direct dev runs)
async def test_unbound_ownership_is_a_noop(SessionLocal):
    assert fence.current_ownership() is None
    await fence.assert_current_owner("graph node node_builder", session_factory=SessionLocal)
    assert await fence.is_current_owner(session_factory=SessionLocal) is True


# ambiguous ownership fails CLOSED
async def test_an_unresolvable_ownership_check_fails_closed(SessionLocal):
    _, own = await _claimed_job(SessionLocal)

    def _broken():
        raise OSError("database unreachable")

    with fence.execution_ownership(own):
        with pytest.raises(fence.StaleWorkerFenced):
            await fence.assert_current_owner("builder tool run_mesh", session_factory=_broken)


# the binding is restored, so a later run cannot inherit a stale owner
async def test_ownership_binding_is_restored_after_the_run(SessionLocal):
    _, own = await _claimed_job(SessionLocal)
    with fence.execution_ownership(own):
        assert fence.current_ownership() is own
    assert fence.current_ownership() is None


async def _superseded(SessionLocal):
    from datetime import datetime, timedelta
    async with SessionLocal() as s:
        job = SimulationJob(owner_id="o", status=JobStatus.pending)
        s.add(job); await s.commit(); jid = job.id
    async with SessionLocal() as s:
        res_a, own_a = await lease.claim_execution(
            s, jid, worker_token=uuid.uuid4(), backend="celery",
            backend_execution_id="exec-A", lease_seconds=1)
        await s.commit()
    assert res_a == ClaimResult.acquired_new_generation
    async with SessionLocal() as s:      # expire in DB state, never by sleeping
        row = await s.get(SimulationJob, jid)
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await s.commit()
    async with SessionLocal() as s:
        res_b, own_b = await lease.claim_execution(
            s, jid, worker_token=uuid.uuid4(), backend="celery", backend_execution_id="exec-B")
        await s.commit()
    assert res_b == ClaimResult.acquired_new_generation
    return jid, own_a, own_b


def _redis_state(job_id) -> dict:
    from meshpipeline.adapters.event_stream.redis import sync_client
    from meshpipeline.events.channels import log_key_for, opkey_set_for, seq_key_for
    r = sync_client()
    return {"seq": r.get(seq_key_for(str(job_id))),
            "backlog": list(r.lrange(log_key_for(str(job_id)), 0, -1)),
            "opkeys": set(r.smembers(opkey_set_for(str(job_id))))}


# an execution-scoped event published by a superseded worker must leave Redis untouched
async def test_a_superseded_worker_publishes_no_durable_event(SessionLocal):
    from meshpipeline.application.execution_publisher import (
        StaleExecutionPublish,
        execution_publisher,
    )

    jid, own_a, own_b = await _superseded(SessionLocal)
    assert own_b.execution_generation == own_a.execution_generation + 1

    op_id = f"stale-probe:{uuid.uuid4()}"            # never emitted, so dedup cannot hide a leak
    before = _redis_state(jid)
    refused = None
    with fence.execution_ownership(own_a, session_factory=SessionLocal):
        try:
            await execution_publisher(str(jid), "geometry_admission").anote(
                "stale worker note", "info", op_id=op_id)
        except StaleExecutionPublish as exc:
            refused = exc
    after = _redis_state(jid)

    assert after["seq"] == before["seq"], (before["seq"], after["seq"])
    assert after["backlog"] == before["backlog"], "a stale worker's note reached the backlog"
    assert after["opkeys"] == before["opkeys"], "a stale worker's operation key was accepted"
    assert not [b for b in after["backlog"]
                if b"stale worker note" in (b if isinstance(b, bytes) else b.encode())]
    assert refused is not None, "the stale publication was not refused"
    assert str(own_a.worker_token) not in str(refused)


async def test_the_current_owner_publishes_exactly_once(SessionLocal):
    from meshpipeline.application.execution_publisher import execution_publisher

    jid, own = await _claimed_job(SessionLocal)
    op_id = f"owned:{uuid.uuid4()}"
    before = _redis_state(jid)
    with fence.execution_ownership(own, session_factory=SessionLocal):
        pub = execution_publisher(str(jid), "geometry_admission")
        await pub.anote("owned note", "info", op_id=op_id)
        await pub.anote("owned note", "info", op_id=op_id)      # same identity, replayed
    after = _redis_state(jid)
    assert len(after["backlog"]) == len(before["backlog"]) + 1, "keyed replay was not idempotent"
    assert len(after["opkeys"] - before["opkeys"]) == 1
    assert [b for b in after["backlog"] if b"owned note" in (b if isinstance(b, bytes) else b.encode())]


@pytest.mark.parametrize("bad", ["missing", "wrong_job"])
async def test_execution_publication_fails_closed_without_valid_ownership(SessionLocal, bad):
    from meshpipeline.application.execution_publisher import (
        StaleExecutionPublish,
        execution_publisher,
    )

    jid, own = await _claimed_job(SessionLocal)
    other, own_other = await _claimed_job(SessionLocal)
    before = _redis_state(jid)
    ctx = None if bad == "missing" else own_other
    with fence.execution_ownership(ctx, session_factory=SessionLocal):
        with pytest.raises(StaleExecutionPublish):
            await execution_publisher(str(jid), "geometry_admission").anote(
                "unowned", "info", op_id=f"probe:{uuid.uuid4()}")
    assert _redis_state(jid) == before, f"{bad} context mutated Redis"


async def test_execution_publication_fails_closed_when_ownership_cannot_be_resolved(SessionLocal):
    from meshpipeline.application.execution_publisher import (
        StaleExecutionPublish,
        execution_publisher,
    )

    jid, own = await _claimed_job(SessionLocal)
    before = _redis_state(jid)

    def _broken():                      # the lookup cannot complete; fence._check fails CLOSED
        raise RuntimeError("database unavailable")
    with fence.execution_ownership(own, session_factory=_broken):
        with pytest.raises(StaleExecutionPublish):
            await execution_publisher(str(jid), "geometry_admission").anote(
                "unresolvable", "info", op_id=f"probe:{uuid.uuid4()}")
    assert _redis_state(jid) == before, "an unresolvable lookup mutated Redis"
    assert str(own.worker_token) not in str(before)


async def test_the_synchronous_unowned_publisher_still_works(SessionLocal):
    # maintenance / terminal / outbox authority - no execution ownership required
    from meshpipeline.contracts.event_stream import publisher

    jid, _ = await _claimed_job(SessionLocal)
    before = _redis_state(jid)
    publisher(str(jid), "geometry_admission").note("maintenance note", "info",
                                        op_id=f"maint:{uuid.uuid4()}")
    after = _redis_state(jid)
    assert len(after["backlog"]) == len(before["backlog"]) + 1
    assert [b for b in after["backlog"] if b"maintenance note" in (b if isinstance(b, bytes) else b.encode())]


async def test_takeover_before_lease_expiry_is_refused(SessionLocal):
    jid, _ = await _claimed_job(SessionLocal)
    async with SessionLocal() as s:
        res, _ = await lease.claim_execution(
            s, jid, worker_token=uuid.uuid4(), backend="celery", backend_execution_id="exec-B")
        await s.commit()
    assert res == ClaimResult.active_lease_conflict


class _NullLog:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


def _engine_callback(job_id):
    import meshpipeline.application.pipeline_run as pr
    # the production callback shape, resolving through the module under test
    async def _announce(name: str) -> None:
        _e = pr.execution_publisher(job_id, "engine_select")
        await _e.astage(op_id="pinned")
        await _e.anote(f"Mesh engine: {name} - the one you asked for", op_id="pinned")
    return _announce


async def test_orchestration_announcements_publish_once_for_the_current_owner(SessionLocal):
    from meshpipeline.pipeline.state_factory import pin_selected_engine

    jid, own = await _claimed_job(SessionLocal)
    before = _redis_state(jid)
    state: dict = {}
    with fence.execution_ownership(own, session_factory=SessionLocal):
        assert await pin_selected_engine(state, "cfmesh", jlog=_NullLog(),
                                         publish=_engine_callback(str(jid))) == "cfmesh"
        await pin_selected_engine(state, "cfmesh", jlog=_NullLog(),
                                  publish=_engine_callback(str(jid)))     # same identity
    after = _redis_state(jid)
    assert len(after["backlog"]) == len(before["backlog"]) + 2
    assert len(after["opkeys"] - before["opkeys"]) == 2
    assert state["engine"] == "cfmesh", "the helper returned before its callback finished"


async def test_orchestration_announcements_are_refused_for_a_superseded_owner(SessionLocal):
    from meshpipeline.application.execution_publisher import StaleExecutionPublish
    from meshpipeline.pipeline.state_factory import pin_selected_engine

    jid, own_a, _ = await _superseded(SessionLocal)
    before = _redis_state(jid)
    state: dict = {}
    refused = None
    with fence.execution_ownership(own_a, session_factory=SessionLocal):
        try:
            await pin_selected_engine(state, "cfmesh", jlog=_NullLog(),
                                      publish=_engine_callback(str(jid)))
        except StaleExecutionPublish as exc:
            refused = exc
    after = _redis_state(jid)
    assert after["seq"] == before["seq"], (before["seq"], after["seq"])
    assert after["backlog"] == before["backlog"], "a superseded worker announced the engine"
    assert after["opkeys"] == before["opkeys"]
    assert refused is not None, "the stale announcement was not refused"


# release: the fence goes with the claim, and the terminal event does not need one


async def test_release_revokes_the_fence_and_the_terminal_event_still_publishes(SessionLocal):
    # THE post-ownership boundary. Release revokes the mirror, so nothing may publish an
    # execution-owned event afterwards - and the terminal event, which is published precisely
    # because the claim is gone, must still land without one and must not reinstall one.
    from meshpipeline.adapters.event_stream.redis import sync_client
    from meshpipeline.contracts.event_stream import publisher
    from meshpipeline.events.channels import fence_key_for

    jid, own = await _claimed_job(SessionLocal)
    r = sync_client()
    try:
        assert r.get(fence_key_for(str(jid))), "the claim installed no fence to revoke"
        before = _redis_state(jid)

        async with SessionLocal() as db:
            await LeaseRepository().release(db, own)
            await db.commit()

        assert not r.exists(fence_key_for(str(jid))), \
            "release left the execution fence standing; a superseded worker could still publish"

        publisher(str(jid), agent="outcome").closing("the run ended", f"t:{jid}")
        after = _redis_state(jid)
        assert len(after["backlog"]) == len(before["backlog"]) + 1, \
            "the terminal event did not reach the backlog after release"
        assert not r.exists(fence_key_for(str(jid))), \
            "terminal publication reinstalled an execution fence"
    finally:
        r.close()
