# Responsibility: Verify concurrent deliveries of one artifact leave a single row, with staleness decided by attempt.
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests import harness_provisioning as hp

import meshpipeline.settings.providers as provcfg
from meshpipeline.persistence.models import (
    Artifact,
    ArtifactType,
    ReconciliationState,
    SimulationJob,
)
from meshpipeline.persistence.repositories.artifact_repository import (
    ArtifactRepository,
    DeliveryOutcome,
)
from meshpipeline.persistence.repositories.reconciliation_repository import ReconciliationRepository

repo = ArtifactRepository()
recon = ReconciliationRepository()


@pytest.fixture()
async def SessionLocal():
    connect_args = {"ssl": True} if provcfg.DB_SSL_REQUIRED else {}
    try:
        engine = create_async_engine(provcfg.POSTGRES_DSN, connect_args=connect_args, pool_size=8)
        # MIGRATION-authoritative schema, rebuilt clean so a stale prior schema cannot linger.
        # It used to be model-authoritative via `create_all`, which builds tables no migration
        # had produced and leaves `alembic_version` empty.
        await hp.reset_schema(provcfg.POSTGRES_DSN)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"PostgreSQL not reachable - PROVISION it ({type(exc).__name__}: {exc})")
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _job(SessionLocal) -> uuid.UUID:
    async with SessionLocal() as s:
        j = SimulationJob(owner_id="owner-1")
        s.add(j)
        await s.commit()
        return j.id


async def _rows(SessionLocal, job_id):
    async with SessionLocal() as s:
        return await repo.get_by_job(s, job_id)


async def _deliver(SessionLocal, job_id, *, key="jobs/j/bundle.tar.gz", checksum="abc", attempt=0,
                   generation=0):
    async with SessionLocal() as s:
        out = await repo.deliver_artifact(
            s, job_id=job_id, logical_key="mesh_bundle", artifact_type=ArtifactType.mesh_bundle,
            storage_key=key, size_bytes=100, checksum=checksum, delivery_attempt=attempt,
            execution_generation=generation)
        await s.commit()
        return out


# same artifact, same checksum - concurrent → one row

async def test_two_concurrent_same_checksum_deliveries_make_one_row(SessionLocal):
    job_id = await _job(SessionLocal)
    a, b = await asyncio.gather(
        _deliver(SessionLocal, job_id, checksum="same"),
        _deliver(SessionLocal, job_id, checksum="same"))
    rows = await _rows(SessionLocal, job_id)
    assert len([r for r in rows if r.artifact_type == ArtifactType.mesh_bundle]) == 1
    # one creator, one idempotent/created - neither is a conflict
    assert {a, b} <= {DeliveryOutcome.created, DeliveryOutcome.idempotent, DeliveryOutcome.updated}
    assert DeliveryOutcome.conflict not in {a, b}


# same identity, different checksum, same attempt - fail closed

async def test_same_attempt_different_checksum_is_a_conflict(SessionLocal):
    job_id = await _job(SessionLocal)
    await _deliver(SessionLocal, job_id, checksum="content-A", attempt=0)
    out = await _deliver(SessionLocal, job_id, checksum="content-B", attempt=0)
    assert out == DeliveryOutcome.conflict          # never silently overwrite the ready deliverable
    rows = await _rows(SessionLocal, job_id)
    bundle = next(r for r in rows if r.artifact_type == ArtifactType.mesh_bundle)
    assert bundle.checksum == "content-A"           # the first (winner) stands


# stale attempt cannot overwrite the winner

async def test_a_stale_attempt_cannot_overwrite_a_newer_attempt(SessionLocal):
    job_id = await _job(SessionLocal)
    await _deliver(SessionLocal, job_id, key="k-new", checksum="new", attempt=2)   # N+1 wins
    out = await _deliver(SessionLocal, job_id, key="k-old", checksum="old", attempt=1)   # N late
    assert out == DeliveryOutcome.superseded
    rows = await _rows(SessionLocal, job_id)
    bundle = next(r for r in rows if r.artifact_type == ArtifactType.mesh_bundle)
    assert bundle.storage_key == "k-new" and bundle.checksum == "new"


async def test_a_newer_attempt_overwrites_an_older_one(SessionLocal):
    job_id = await _job(SessionLocal)
    await _deliver(SessionLocal, job_id, key="k-old", checksum="old", attempt=0)
    out = await _deliver(SessionLocal, job_id, key="k-new", checksum="new", attempt=1)
    assert out in (DeliveryOutcome.updated, DeliveryOutcome.created)
    rows = await _rows(SessionLocal, job_id)
    bundle = next(r for r in rows if r.artifact_type == ArtifactType.mesh_bundle)
    assert bundle.checksum == "new" and bundle.delivery_attempt == 1


# delivery currency is (execution_generation, delivery_attempt)

async def test_a_superseded_generation_cannot_overwrite_a_newer_generations_mesh(SessionLocal):
    job_id = await _job(SessionLocal)
    await _deliver(SessionLocal, job_id, key="k-gen2", checksum="gen2", attempt=0, generation=2)
    out = await _deliver(SessionLocal, job_id, key="k-gen1", checksum="gen1", attempt=9, generation=1)
    assert out == DeliveryOutcome.superseded
    rows = await _rows(SessionLocal, job_id)
    bundle = next(r for r in rows if r.artifact_type == ArtifactType.mesh_bundle)
    assert bundle.storage_key == "k-gen2" and bundle.checksum == "gen2"
    assert bundle.execution_generation == 2


async def test_a_newer_generation_overwrites_an_older_generations_mesh(SessionLocal):
    job_id = await _job(SessionLocal)
    await _deliver(SessionLocal, job_id, key="k-old", checksum="old", attempt=7, generation=1)
    out = await _deliver(SessionLocal, job_id, key="k-new", checksum="new", attempt=0, generation=2)
    assert out in (DeliveryOutcome.updated, DeliveryOutcome.created)
    rows = await _rows(SessionLocal, job_id)
    bundle = next(r for r in rows if r.artifact_type == ArtifactType.mesh_bundle)
    assert bundle.checksum == "new" and bundle.execution_generation == 2


# the DB unique constraint is the final authority (raw concurrent inserts)

async def test_the_unique_constraint_rejects_a_raw_duplicate(SessionLocal):
    job_id = await _job(SessionLocal)
    async with SessionLocal() as s:
        s.add(Artifact(job_id=job_id, artifact_type=ArtifactType.mesh_bundle,
                       logical_key="mesh_bundle", storage_key="k1", size_bytes=1))
        await s.commit()
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):
        async with SessionLocal() as s:
            s.add(Artifact(job_id=job_id, artifact_type=ArtifactType.mesh_bundle,
                           logical_key="mesh_bundle", storage_key="k2", size_bytes=2))
            await s.commit()


# durable orphan record + idempotency

async def test_orphan_record_is_durable_and_idempotent(SessionLocal):
    job_id = await _job(SessionLocal)
    async with SessionLocal() as s:
        for _ in range(3):   # a duplicate report must not create duplicate records
            await recon.record_orphan(
                s, owner_id="owner-1", job_id=job_id, delivery_attempt=0, logical_key="mesh_bundle",
                artifact_type=ArtifactType.mesh_bundle, object_key="jobs/j/bundle.tar.gz",
                object_checksum="etag-1", object_size=100)
        await s.commit()
    async with SessionLocal() as s:
        recs = await recon.get_by_job(s, job_id)
    assert len(recs) == 1 and recs[0].state == ReconciliationState.pending
    assert recs[0].owner_id == "owner-1" and recs[0].object_checksum == "etag-1"


# winning retry before reconciliation - reconciler must NOT delete the winner

async def test_reconciler_does_not_delete_an_object_a_ready_row_owns(SessionLocal, monkeypatch):
    job_id = await _job(SessionLocal)
    key = "jobs/j/bundle.tar.gz"
    # a durable orphan for attempt 0
    async with SessionLocal() as s:
        await recon.record_orphan(s, owner_id="owner-1", job_id=job_id, delivery_attempt=0,
                                  logical_key="mesh_bundle", artifact_type=ArtifactType.mesh_bundle,
                                  object_key=key, object_checksum="etag-1", object_size=100)
        await s.commit()
    # a WINNING retry then registers a ready row pointing at the same key
    await _deliver(SessionLocal, job_id, key=key, checksum="etag-1", attempt=1)

    # a fake store that would record any delete
    deleted: list[str] = []

    class _Store:
        def exists(self, *, object_key): return True
        def delete_object(self, *, object_key): deleted.append(object_key)
        def object_checksum(self, *, object_key): return "etag-1"

    import meshpipeline.contracts.object_storage as osmod
    monkeypatch.setattr(osmod, "get_object_store", lambda: _Store())

    from meshpipeline.application.maintenance.reconcile import _reconcile_async
    counts = await _reconcile_async()
    assert deleted == [], "the reconciler must never delete an object a ready row owns"
    # the orphan is resolved as adopted (the winner owns it), not deleted
    async with SessionLocal() as s:
        recs = await recon.get_by_job(s, job_id)
    assert recs[0].state == ReconciliationState.resolved_adopted
    assert counts["adopted"] >= 1


# DB-through-HTTP - one logical artifact listed once; reconciliation never exposed

async def test_api_lists_one_download_per_logical_artifact_and_hides_reconciliation(SessionLocal, monkeypatch):
    import meshpipeline.api.v1.simulation as sim
    from meshpipeline.persistence.models import JobStatus

    job_id = await _job(SessionLocal)
    async with SessionLocal() as s:
        # one bundle + one preview (the only logical artifacts today)
        await repo.deliver_artifact(s, job_id=job_id, logical_key="mesh_bundle",
                                    artifact_type=ArtifactType.mesh_bundle, storage_key="k-bundle",
                                    size_bytes=10, checksum="c1", delivery_attempt=0)
        await repo.deliver_artifact(s, job_id=job_id, logical_key="mesh",
                                    artifact_type=ArtifactType.mesh, storage_key="k-mesh",
                                    size_bytes=5, checksum="c2", delivery_attempt=0)
        # a reconciliation record for a would-be orphan - must NEVER be user-downloadable
        await recon.record_orphan(s, owner_id="owner-1", job_id=job_id, delivery_attempt=0,
                                  logical_key="mesh_bundle", artifact_type=ArtifactType.mesh_bundle,
                                  object_key="k-orphan", object_checksum="cx", object_size=1)
        _job_row = await s.get(SimulationJob, job_id)
        _job_row.status = JobStatus.succeeded
        await s.commit()

    # a fake signed-URL maker records which keys get URLs
    signed: list[str] = []

    class _Svc:
        async def get_job(self, db, jid, owner, *, organization_id=""):
            # Mirrors JobService.get_job's real signature: the route resolves the organisation
            # beside the owner and passes it, so a stub that omits it fails the CALL rather than
            # the assertion - which is how this went unnoticed until the integration tier ran.
            from meshpipeline.application.job_service import JobService
            return await JobService().get_job(db, jid, owner,
                                              organization_id=organization_id)
        async def signed_url(self, storage_key):
            signed.append(storage_key)
            return f"https://signed/{storage_key}"

    monkeypatch.setattr(sim, "svc", _Svc())
    monkeypatch.setattr(sim, "get_db", SessionLocal_ctx(SessionLocal))
    # the engine now comes from the durable viewer payload, not a workspace read
    async def _vdata(job_id, owner_id, db=None, *, organization_id=""):
        # Mirrors _viewer_data_or_empty's real signature, organisation included. A stub that
        # omits a parameter the route passes fails the CALL rather than the assertion, so the
        # test reports a TypeError from inside the route instead of the behaviour it is about.
        return {"engine": "gmsh", "mesh_available": True,
                "review": {"verdict": "PASS"}}
    monkeypatch.setattr(sim, "_viewer_data_or_empty", _vdata)


    resp = await sim.get_job(job_id, owner_id="owner-1")
    keys = [str(a.download_url) for a in resp.artifacts]
    # exactly two logical artifacts, each once; the orphan key is NOT among them
    assert len(resp.artifacts) == 2
    assert not any("k-orphan" in k for k in keys), "a reconciliation object must not be downloadable"
    assert sorted(signed) == ["k-bundle", "k-mesh"]

    # wrong owner sees nothing
    from fastapi import HTTPException
    try:
        await sim.get_job(job_id, owner_id="someone-else")
        raise AssertionError("cross-owner access must 404")
    except HTTPException as exc:
        assert exc.status_code == 404


def SessionLocal_ctx(SessionLocal):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _ctx():
        async with SessionLocal() as s:
            yield s
    return _ctx
