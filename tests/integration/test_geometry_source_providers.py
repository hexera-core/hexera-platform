# Responsibility: Verify a geometry source round-trips through the real repository and store, refusing altered bytes.
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import pytest
from tests._geometry_support import persisted_geometry

from meshpipeline.contracts.geometry_units import LengthUnit, ResolutionBasis

pytestmark = pytest.mark.asyncio

_DSN = os.getenv("DATABASE_URL", "")
_MINIO = os.getenv("MINIO_ENDPOINT", "")

pytest.importorskip("minio")
if not _DSN or not _MINIO:
    pytest.skip("real PostgreSQL and MinIO endpoints are required "
                "(DATABASE_URL, MINIO_ENDPOINT)", allow_module_level=True)


def _bytes(marker: str, size: int = 4096) -> bytes:
    body = f"ISO-10303-21;/* {marker} */\n".encode()
    return body + bytes((i * 7 + len(body)) % 251 for i in range(max(0, size - len(body))))


def _digest(payload: bytes) -> tuple[str, int]:
    return hashlib.sha256(payload).hexdigest(), len(payload)


@pytest.fixture()
def store():
    from meshpipeline.adapters.object_storage.factory import build_object_store
    from meshpipeline.contracts import object_storage
    st = build_object_store()
    object_storage.set_object_store(st)
    yield st
    object_storage.set_object_store(None)


@pytest.fixture()
async def db():
    from meshpipeline.persistence.session import dispose_engine, get_db
    async with get_db() as session:
        yield session
    await dispose_engine()


async def _make_source(db, store, *, owner: str, marker: str, size: int = 4096,
                       filename: str = "duct.step",
                       unit: LengthUnit = LengthUnit.millimetre,
                       basis: ResolutionBasis = ResolutionBasis.file_declared):
    return await persisted_geometry(db, store=store, tmp_path=Path(os.environ["API_ROOT"]) /
                                    f"staged-{marker}-{uuid.uuid4().hex[:8]}", owner_id=owner,
                                    filename=filename, marker=marker, size=size,
                                    unit=unit, basis=basis)


# schema and repositories

async def test_the_live_schema_has_no_path_column(db):
    from sqlalchemy import text
    rows = await db.execute(text(
        "select table_name, column_name from information_schema.columns "
        "where column_name like '%step_file%' or column_name like '%file_path%'"))
    assert rows.fetchall() == []


async def test_both_owners_of_geometry_reference_the_catalog(db):
    from sqlalchemy import text
    rows = await db.execute(text(
        "select table_name from information_schema.columns "
        "where column_name = 'geometry_source_id' order by table_name"))
    # A session and a job reference the geometry they are meshing; an interpretation references
    # the geometry whose physical scale it records. All three point AT the catalog, and none of
    # them holds a copy of the bytes or a path to them.
    assert [r[0] for r in rows.fetchall()] == [
        "chat_sessions", "geometry_interpretations", "simulation_jobs"]


async def test_a_source_round_trips_through_the_real_repository(db, store):
    from meshpipeline.persistence.repositories.geometry_source_repository import (
        GeometrySourceRepository,
    )
    geom = await _make_source(db, store, owner="tenant-a", marker="round-trip")
    row = await GeometrySourceRepository().get_for_owner(db, uuid.UUID(geom.ref.source_id), "tenant-a")
    assert row is not None
    assert row.sha256 == geom.ref.sha256 and row.size_bytes == len(geom.payload)


async def test_another_tenant_cannot_resolve_the_source(db, store):
    from meshpipeline.persistence.repositories.geometry_source_repository import (
        GeometrySourceRepository,
    )
    geom = await _make_source(db, store, owner="tenant-a", marker="private")
    assert await GeometrySourceRepository().get_for_owner(
        db, uuid.UUID(geom.ref.source_id), "tenant-b") is None


# real object storage

async def test_the_object_lands_under_the_sources_prefix(db, store):
    geom = await _make_source(db, store, owner="tenant-a", marker="prefix")
    assert geom.ref.object_key.startswith("sources/")
    assert store.exists(object_key=geom.ref.object_key)


async def test_materialisation_downloads_and_verifies_real_bytes(db, store, tmp_path):
    from meshpipeline.application.geometry_materializer import materialize_for_job
    geom = await _make_source(db, store, owner="tenant-a", marker="verified")
    mg = await materialize_for_job(db, geom.ref, geom.interpretation_snapshot,
                                   workspace=tmp_path / "ws", job_id="j-1")
    assert Path(mg.path).read_bytes() == geom.payload          # the bytes, from MinIO
    assert tmp_path in Path(mg.path).parents
    assert mg.ref.sha256 == geom.ref.sha256


# real integrity matrix

async def _rejects(db, ref, interpretation, tmp_path, expected):
    from meshpipeline.application.geometry_materializer import materialize_for_job
    from meshpipeline.contracts.geometry_source import GeometrySourceError
    with pytest.raises(GeometrySourceError) as exc:
        await materialize_for_job(db, ref, interpretation, workspace=tmp_path / "ws",
                                  job_id="j")
    assert exc.value.failure_class is expected, str(exc.value)
    return exc.value


async def test_a_vanished_object_is_an_integrity_failure(db, store, tmp_path):
    from meshpipeline.errors import FailureClass
    geom = await _make_source(db, store, owner="tenant-a", marker="gone")
    store.delete_object(object_key=geom.ref.object_key)         # disposable state, deliberately
    await _rejects(db, geom.ref, geom.interpretation_snapshot, tmp_path, FailureClass.DATA_INTEGRITY)


@pytest.mark.parametrize("corruption", ["truncated", "same_size_altered", "different_size"])
async def test_altered_stored_bytes_are_an_integrity_failure(db, store, tmp_path, corruption):
    from meshpipeline.errors import FailureClass
    geom = await _make_source(db, store, owner="tenant-a", marker="original")
    swapped = {"truncated": geom.payload[:-16],
               "same_size_altered": bytes(b ^ 0xFF for b in geom.payload),
               "different_size": geom.payload + b"appended"}[corruption]
    geom.local_path.write_bytes(swapped)
    store.upload_file(local_path=geom.local_path, object_key=geom.ref.object_key)   # overwrite in place
    await _rejects(db, geom.ref, geom.interpretation_snapshot, tmp_path, FailureClass.DATA_INTEGRITY)


@pytest.mark.parametrize("field,value", [
    ("sha256", "b" * 64),
    ("size_bytes", 999999),
    ("object_key", "sources/not-the-approved-object"),
    ("original_filename", "renamed.step"),
    ("suffix_hint", ".stl"),
])
async def test_a_dispatch_snapshot_disagreeing_with_the_row_is_refused(db, store, tmp_path,
                                                                      field, value):
    import dataclasses

    from meshpipeline.errors import FailureClass
    geom = await _make_source(db, store, owner="tenant-a", marker="drift")
    await _rejects(db, dataclasses.replace(geom.ref, **{field: value}), geom.interpretation_snapshot,
                   tmp_path,
                   FailureClass.DATA_INTEGRITY)


async def test_a_snapshot_naming_another_tenant_is_not_authorized(db, store, tmp_path):
    import dataclasses

    from meshpipeline.errors import FailureClass
    geom = await _make_source(db, store, owner="tenant-a", marker="tenant")
    await _rejects(db, dataclasses.replace(geom.ref, owner_id="tenant-b"),
                   geom.interpretation_snapshot, tmp_path, FailureClass.NOT_AUTHORIZED)


async def test_a_snapshot_naming_an_unknown_source_is_not_authorized(db, store, tmp_path):
    import dataclasses

    from meshpipeline.errors import FailureClass
    geom = await _make_source(db, store, owner="tenant-a", marker="unknown")
    await _rejects(db, dataclasses.replace(geom.ref, source_id=str(uuid.uuid4())),
                   geom.interpretation_snapshot, tmp_path, FailureClass.NOT_AUTHORIZED)


async def test_a_malformed_source_id_is_an_internal_failure(db, store, tmp_path):
    import dataclasses

    from meshpipeline.errors import FailureClass
    geom = await _make_source(db, store, owner="tenant-a", marker="malformed")
    await _rejects(db, dataclasses.replace(geom.ref, source_id="not-a-uuid"), geom.interpretation_snapshot,
                   tmp_path,
                   FailureClass.INTERNAL)


async def test_a_tampered_row_digest_is_caught_by_reconciliation(db, store, tmp_path):
    from sqlalchemy import text

    from meshpipeline.errors import FailureClass
    geom = await _make_source(db, store, owner="tenant-a", marker="tamper")
    await db.execute(text("update geometry_sources set sha256 = :s where id = :i"),
                     {"s": "c" * 64, "i": uuid.UUID(geom.ref.source_id)})
    await db.commit()
    await _rejects(db, geom.ref, geom.interpretation_snapshot, tmp_path, FailureClass.DATA_INTEGRITY)


async def test_an_unreachable_provider_is_a_dependency_failure(db, store, tmp_path, monkeypatch):
    from meshpipeline.contracts.object_storage import StorageError
    from meshpipeline.errors import FailureClass
    geom = await _make_source(db, store, owner="tenant-a", marker="down")

    def _boom(**_kw):
        raise StorageError(
            "s3://prod-geometry/sources/x AccessDenied arn:aws:iam::9:user/svc key=AKIAEXAMPLE")

    monkeypatch.setattr(store, "download_file", _boom)
    exc = await _rejects(db, geom.ref, geom.interpretation_snapshot, tmp_path, FailureClass.DEPENDENCY_DOWN)
    text_ = str(exc)
    for leak in ("s3://", "AccessDenied", "arn:", "AKIA", geom.ref.object_key, geom.ref.sha256,
                 geom.ref.owner_id, "prod-geometry"):
        assert leak not in text_


# compensation

async def test_compensation_removes_only_the_exact_new_object(db, store):
    from meshpipeline.contracts.geometry_source import source_object_key
    keep = await _make_source(db, store, owner="tenant-a", marker="keep")

    orphan_id = uuid.uuid4()
    orphan_key = source_object_key(orphan_id)
    local = Path(os.environ["API_ROOT"]) / "orphan.bin"
    local.write_bytes(_bytes("orphan"))
    store.upload_file(local_path=local, object_key=orphan_key)

    store.delete_object(object_key=orphan_key)             # the exact-key compensation
    assert not store.exists(object_key=orphan_key)
    assert store.exists(object_key=keep.ref.object_key)    # the neighbour survives


# separate processes

async def test_reconstruction_succeeds_in_a_process_with_no_access_to_the_api_root(
        db, store, tmp_path):
    geom = await _make_source(db, store, owner="tenant-a", marker="cross-process", size=8192)

    api_root = Path(os.environ["API_ROOT"])
    os.chmod(api_root, 0o000)                              # deny the child process entirely
    try:
        pipeline_root = tmp_path / "pipeline-root"
        pipeline_root.mkdir()
        script = textwrap.dedent("""
            import asyncio, json, os, sys
            from pathlib import Path
            from meshpipeline.adapters.object_storage.factory import build_object_store
            from meshpipeline.contracts import object_storage
            from meshpipeline.contracts.geometry_source import (
                GeometryInterpretationRef,
                GeometrySourceRef,
            )
            from meshpipeline.application.geometry_materializer import materialize_for_job
            from meshpipeline.persistence.session import get_db

            async def main():
                object_storage.set_object_store(build_object_store())
                ref = GeometrySourceRef.from_payload(json.loads(sys.argv[1]))
                interp = GeometryInterpretationRef.from_payload(json.loads(sys.argv[3]))
                async with get_db() as db:
                    mg = await materialize_for_job(
                        db, ref, interp, workspace=Path(sys.argv[2]), job_id="cross")
                print(json.dumps({
                    "pid": os.getpid(), "cwd": os.getcwd(), "path": str(mg.path),
                    "sha_of_file": __import__("hashlib").sha256(
                        Path(mg.path).read_bytes()).hexdigest(),
                    "ref_sha": mg.ref.sha256, "source_id": mg.ref.source_id}))
            asyncio.run(main())
        """)
        proc = subprocess.run(
            [sys.executable, "-c", script, json.dumps(geom.ref.to_payload()),
             str(pipeline_root), json.dumps(geom.interpretation_snapshot.to_payload())],
            cwd=pipeline_root, capture_output=True, text=True, timeout=120,
            env={**os.environ, "PYTHONPATH": os.environ.get("PYTHONPATH", "")})
        assert proc.returncode == 0, proc.stderr[-2000:]
        out = json.loads(proc.stdout.strip().splitlines()[-1])
    finally:
        os.chmod(api_root, 0o755)

    assert out["pid"] != os.getpid()                       # genuinely another process
    assert Path(out["cwd"]).resolve() == pipeline_root.resolve()
    delivered = Path(out["path"])
    assert pipeline_root in delivered.parents              # its own root, not the API's
    assert str(geom.local_path) != out["path"]
    assert out["sha_of_file"] == _digest(geom.payload)[0]       # byte-identical to what was uploaded
    assert out["ref_sha"] == geom.ref.sha256
    assert out["source_id"] == geom.ref.source_id


# real Redis run events

async def test_run_events_traverse_redis_and_replay_for_a_reconnecting_client(db, store):
    import os as _os
    if not _os.getenv("REDIS_URL"):
        pytest.skip("REDIS_URL is required for the Redis seam")

    from meshpipeline.adapters._shared.redis_client import sync_client
    from meshpipeline.adapters.event_stream.redis import (
        JobPublisher,
        RedisEventSubscription,
        log_key_for,
    )

    geom = await _make_source(db, store, owner="tenant-a", marker="events")
    job_id = str(uuid.uuid4())

    pub = JobPublisher(job_id, agent="geometry_admission")
    pub.stage()
    pub.note("Preparing the approved geometry.", "info")
    pub.closing("Mesh generation finished.")

    # the keys really exist in Redis, written by production code
    assert sync_client().exists(log_key_for(job_id))

    sub = RedisEventSubscription(job_id)
    await sub.open()
    try:
        backlog = await sub.backlog()
    finally:
        await sub.close()

    assert len(backlog) >= 3, backlog
    blob = " ".join(backlog)
    assert "Preparing the approved geometry." in blob
    # the run's event stream carries no source identity or storage detail
    for leak in (geom.ref.object_key, geom.ref.sha256, geom.ref.source_id, "sources/", "geomcut-sources"):
        assert leak not in blob
