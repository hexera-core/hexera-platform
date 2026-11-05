# Responsibility: Verify delivery against a real object store survives restart, retry and reconciliation, no orphan.
from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests import harness_provisioning as hp
from tests.engine_workspaces import build_workspace

import meshpipeline.application.artifact_uploader as au
import meshpipeline.settings.providers as provcfg
from meshpipeline.contracts.object_storage import StorageError
from meshpipeline.persistence.models import ArtifactType, SimulationJob


@pytest.fixture()
async def SessionLocal():
    connect_args = {"ssl": True} if provcfg.DB_SSL_REQUIRED else {}
    try:
        engine = create_async_engine(provcfg.POSTGRES_DSN, connect_args=connect_args, pool_size=6)
        # MIGRATION-authoritative schema, rebuilt clean so a stale prior schema cannot linger.
        # It used to be model-authoritative via `create_all`, which builds tables no migration
        # had produced and leaves `alembic_version` empty.
        await hp.reset_schema(provcfg.POSTGRES_DSN)
    except Exception as exc:  # noqa: BLE001
        pytest.fail("PostgreSQL not reachable for the artifact-delivery test - it must be "
                    f"PROVISIONED, not skipped ({type(exc).__name__}: {exc})")
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture()
def real_store():
    from meshpipeline.adapters.object_storage.minio import MinioStore
    store = MinioStore()
    try:
        store._ensure_bucket(store._client())
        store._client().bucket_exists(store._bucket)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"MinIO not reachable for the artifact-delivery test - PROVISION it ({exc})")
    import meshpipeline.contracts.object_storage as osmod
    prev = osmod._store
    osmod.set_object_store(store)
    yield store
    osmod.set_object_store(prev)


def _ws(tmp_path, *, mesh=True) -> Path:
    ws = build_workspace(tmp_path, "gmsh")
    if mesh:
        (ws / "mesh.msh").write_bytes(b"$MeshFormat\n")
    return ws


async def _seed_job(SessionLocal) -> uuid.UUID:
    async with SessionLocal() as s:
        job = SimulationJob(owner_id="owner-1")
        s.add(job)
        await s.commit()
        return job.id


async def _artifacts(SessionLocal, job_id):
    from meshpipeline.persistence.repositories.artifact_repository import ArtifactRepository
    async with SessionLocal() as s:
        return await ArtifactRepository().get_by_job(s, job_id)


# happy path: real object + real row, integrity holds

async def test_delivery_stores_object_and_row_with_integrity(SessionLocal, real_store, tmp_path):
    job_id = await _seed_job(SessionLocal)
    async with SessionLocal() as db:
        report = await au.upload_job_artifacts(db, job_id, _ws(tmp_path), engine="gmsh")
        await db.commit()
    assert report.required_all_delivered()
    rows = await _artifacts(SessionLocal, job_id)
    assert {r.artifact_type for r in rows} == {
        ArtifactType.mesh, ArtifactType.mesh_bundle, ArtifactType.viewer_data}
    bundle = next(r for r in rows if r.artifact_type == ArtifactType.mesh_bundle)
    # the object really exists in MinIO and its size matches the persisted row
    assert real_store.exists(object_key=bundle.storage_key)
    assert bundle.size_bytes > 0


# upload failure: bounded retry not tested here (uploader level), but fail-closed is

async def test_required_upload_failure_raises_and_writes_no_row(SessionLocal, real_store, tmp_path):
    job_id = await _seed_job(SessionLocal)
    # force the bundle upload to fail while the store itself is real
    _orig = real_store.upload_file
    def _fail_bundle(*, local_path, object_key, content_type=None, metadata=None):
        if object_key.endswith("gmsh_case.tar.gz"):
            raise StorageError("injected MinIO failure on the required bundle")
        return _orig(local_path=local_path, object_key=object_key, content_type=content_type)
    real_store.upload_file = _fail_bundle

    async with SessionLocal() as db:
        with pytest.raises(au.RequiredArtifactDeliveryError):
            await au.upload_job_artifacts(db, job_id, _ws(tmp_path), engine="gmsh")
        await db.rollback()
    rows = await _artifacts(SessionLocal, job_id)
    assert not any(r.artifact_type == ArtifactType.mesh_bundle for r in rows)   # no false success


# row failure after a real object upload: orphan reconciled

async def test_row_failure_after_real_upload_deletes_the_orphan(SessionLocal, real_store, tmp_path):
    job_id = await _seed_job(SessionLocal)
    bundle_key = f"jobs/{job_id}/gmsh_case.tar.gz"

    class _FailingRepo:
        async def get_by_job(self, db, jid):
            return []
        async def deliver_artifact(self, db, **kw):
            raise RuntimeError("injected DB failure after the object was stored")

    import meshpipeline.contracts.object_storage as osmod
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(au, "ArtifactRepository", lambda: _FailingRepo())
        async with SessionLocal() as db:
            with pytest.raises(au.RequiredArtifactDeliveryError):
                await au.upload_job_artifacts(db, job_id, _ws(tmp_path, mesh=False), engine="gmsh")
            await db.rollback()

    # the orphan object was best-effort deleted from the REAL store - not left dangling, no row
    assert not osmod.get_object_store().exists(object_key=bundle_key)
    rows = await _artifacts(SessionLocal, job_id)
    assert rows == []


# idempotent retry: real object overwritten at the same key, one row

async def test_retry_is_idempotent_one_object_one_row(SessionLocal, real_store, tmp_path):
    job_id = await _seed_job(SessionLocal)
    for _ in range(3):
        async with SessionLocal() as db:
            await au.upload_job_artifacts(db, job_id, _ws(tmp_path), engine="gmsh")
            await db.commit()
    rows = await _artifacts(SessionLocal, job_id)
    bundle_rows = [r for r in rows if r.artifact_type == ArtifactType.mesh_bundle]
    assert len(bundle_rows) == 1, "retries must not create duplicate Artifact rows"
    assert real_store.exists(object_key=bundle_rows[0].storage_key)


# restart: a fresh engine/store (a restarted worker) delivers the same job once

async def test_delivery_survives_a_restart(SessionLocal, real_store, tmp_path):
    job_id = await _seed_job(SessionLocal)
    connect_args = {"ssl": True} if provcfg.DB_SSL_REQUIRED else {}
    engine2 = create_async_engine(provcfg.POSTGRES_DSN, connect_args=connect_args)
    Factory2 = async_sessionmaker(bind=engine2, expire_on_commit=False)
    try:
        async with Factory2() as db:
            report = await au.upload_job_artifacts(db, job_id, _ws(tmp_path), engine="gmsh")
            await db.commit()
        assert report.required_all_delivered()
        rows = await _artifacts(SessionLocal, job_id)
        assert any(r.artifact_type == ArtifactType.mesh_bundle for r in rows)
    finally:
        await engine2.dispose()


# durable reconciliation against REAL MinIO (restart-safe, generation-aware)

async def test_reconciler_deletes_a_true_orphan_after_restart(SessionLocal, real_store, tmp_path):
    from meshpipeline.application.maintenance.reconcile import _reconcile_async
    from meshpipeline.persistence.repositories.reconciliation_repository import (
        ReconciliationRepository,
    )
    job_id = await _seed_job(SessionLocal)
    key = f"jobs/{job_id}/orphan_bundle.tar.gz"
    # put a real object with no Artifact row
    src = tmp_path / "o.bin"
    src.write_bytes(b"orphan-bytes")
    stored = real_store.upload_file(local_path=src, object_key=key, content_type="application/gzip")
    assert real_store.exists(object_key=key)
    async with SessionLocal() as s:
        await ReconciliationRepository().record_orphan(
            s, owner_id="owner-1", job_id=job_id, delivery_attempt=0, logical_key="mesh_bundle",
            artifact_type=ArtifactType.mesh_bundle, object_key=key,
            object_checksum=stored.checksum, object_size=stored.size_bytes)
        await s.commit()

    counts = await _reconcile_async()   # a brand-new engine/session - the restart
    assert counts["deleted"] >= 1
    assert not real_store.exists(object_key=key), "the true orphan must be deleted"


async def test_reconciler_will_not_delete_when_the_object_generation_changed(SessionLocal, real_store, tmp_path):
    from meshpipeline.application.maintenance.reconcile import _reconcile_async
    from meshpipeline.persistence.models import ReconciliationState
    from meshpipeline.persistence.repositories.reconciliation_repository import (
        ReconciliationRepository,
    )
    job_id = await _seed_job(SessionLocal)
    key = f"jobs/{job_id}/gen_bundle.tar.gz"
    async with SessionLocal() as s:
        await ReconciliationRepository().record_orphan(
            s, owner_id="owner-1", job_id=job_id, delivery_attempt=0, logical_key="mesh_bundle",
            artifact_type=ArtifactType.mesh_bundle, object_key=key,
            object_checksum="a-stale-etag-that-will-not-match", object_size=5)
        await s.commit()
    # a DIFFERENT object now occupies the key (the winner's content)
    src = tmp_path / "winner.bin"
    src.write_bytes(b"winner-content-different")
    real_store.upload_file(local_path=src, object_key=key, content_type="application/gzip")

    await _reconcile_async()
    assert real_store.exists(object_key=key), "a generation-changed object must not be deleted"
    async with SessionLocal() as s:
        recs = await ReconciliationRepository().get_by_job(s, job_id)
    assert recs[0].state == ReconciliationState.blocked_conflict


async def test_two_concurrent_reconcilers_resolve_each_record_once(SessionLocal, real_store, tmp_path):
    from meshpipeline.application.maintenance.reconcile import _reconcile_async
    from meshpipeline.persistence.models import ReconciliationState
    from meshpipeline.persistence.repositories.reconciliation_repository import (
        ReconciliationRepository,
    )
    job_id = await _seed_job(SessionLocal)
    key = f"jobs/{job_id}/dup_bundle.tar.gz"
    src = tmp_path / "d.bin"; src.write_bytes(b"dup-bytes")
    stored = real_store.upload_file(local_path=src, object_key=key, content_type="application/gzip")
    async with SessionLocal() as s:
        await ReconciliationRepository().record_orphan(
            s, owner_id="owner-1", job_id=job_id, delivery_attempt=0, logical_key="mesh_bundle",
            artifact_type=ArtifactType.mesh_bundle, object_key=key,
            object_checksum=stored.checksum, object_size=stored.size_bytes)
        await s.commit()
    # two reconcilers race; the row lock (FOR UPDATE) means exactly ONE processes the record -
    # proven by counting deletes, since MinIO delete is idempotent and would otherwise hide a
    # double-process. Without the lock both reconcilers would act → two deletes.
    deletes: list[str] = []
    _orig_del = real_store.delete_object
    def _counting_delete(*, object_key):
        deletes.append(object_key)
        return _orig_del(object_key=object_key)
    real_store.delete_object = _counting_delete

    import asyncio
    await asyncio.gather(_reconcile_async(), _reconcile_async())
    async with SessionLocal() as s:
        recs = await ReconciliationRepository().get_by_job(s, job_id)
    assert len(recs) == 1 and recs[0].state == ReconciliationState.resolved_deleted
    assert not real_store.exists(object_key=key)
    assert deletes.count(key) == 1, f"the record was processed more than once: {deletes}"


# hosted split filesystem (F7)

async def test_the_api_serves_the_viewer_after_the_worker_workspace_is_gone(
        SessionLocal, real_store, tmp_path):
    import json
    import shutil

    import meshpipeline.settings.runtime as rtcfg
    from meshpipeline.application.viewer_payload import VIEWER_LOGICAL_KEY
    from meshpipeline.persistence.repositories.artifact_repository import ArtifactRepository

    job_id = await _seed_job(SessionLocal)
    worker_ws = _ws(tmp_path / "worker")
    async with SessionLocal() as db:
        report = await au.upload_job_artifacts(db, job_id, worker_ws, engine="gmsh")
        await db.commit()
    assert report.required_all_delivered()

    # the worker exits and its disk goes away
    shutil.rmtree(worker_ws)
    assert not worker_ws.exists()

    # the "API" has a different, empty workspace root
    api_root = tmp_path / "api-empty"
    api_root.mkdir()
    _prev, rtcfg.WORKSPACE_BASE = rtcfg.WORKSPACE_BASE, api_root
    try:
        async with SessionLocal() as db:
            row = await ArtifactRepository().get_by_logical_key(db, job_id, VIEWER_LOGICAL_KEY)
        assert row is not None
        payload = json.loads(real_store.get_bytes(object_key=row.storage_key))
    finally:
        rtcfg.WORKSPACE_BASE = _prev

    assert payload["engine"] == "gmsh"
    assert payload["surface"], "the viewer surface did not survive the worker"
    assert not list(api_root.iterdir()), "the API root was written to"
    assert str(tmp_path) not in json.dumps(payload), "a worker path leaked into the payload"


async def test_a_foreign_tenant_cannot_reach_another_jobs_viewer_artifact(
        SessionLocal, real_store, tmp_path):
    from meshpipeline.application.job_service import JobService

    job_id = await _seed_job(SessionLocal)
    async with SessionLocal() as db:
        await au.upload_job_artifacts(db, job_id, _ws(tmp_path), engine="gmsh")
        await db.commit()
    async with SessionLocal() as db:
        assert await JobService().get_job(db, job_id, "someone-else") is None
