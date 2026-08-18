# Responsibility: Verify object keys refuse traversal, and a required upload failure writes no row.
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import meshpipeline.settings.providers as provcfg

APP = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"

from tests.engine_workspaces import build_workspace

from meshpipeline.adapters.object_storage import (  # noqa: E402
    StorageError,
    StoredObject,
    normalize_key,
)

# factory


def test_object_keys_reject_traversal_and_absolute_paths():
    for bad in ("../etc/passwd", "/abs/path", "jobs/../../x", "a\\b", "C:/x", "", "jobs//x", "jobs/./x"):
        with pytest.raises(StorageError):
            normalize_key(bad)
    assert normalize_key("jobs/abc/mesh.msh") == "jobs/abc/mesh.msh"


# MinIO: unchanged behavior
def test_minio_upload_and_signed_url(monkeypatch, tmp_path):
    monkeypatch.setattr(provcfg, "MINIO_BUCKET", "mesh-artifacts")
    from meshpipeline.adapters.object_storage.minio import MinioStore
    calls = {}
    fake = SimpleNamespace(
        bucket_exists=lambda b: True, make_bucket=lambda b: None,
        fput_object=lambda **k: calls.update(fput=k) or SimpleNamespace(etag="abc"),
        presigned_get_object=lambda b, k, expires=None: calls.update(url=(b, k, expires)) or "http://minio/sign")
    store = MinioStore()
    monkeypatch.setattr(store, "_client", lambda: fake)
    f = tmp_path / "m.msh"; f.write_bytes(b"data")
    so = store.upload_file(local_path=f, object_key="jobs/j/m.msh", content_type="application/octet-stream")
    assert isinstance(so, StoredObject) and so.object_key == "jobs/j/m.msh" and so.size_bytes == 4
    assert calls["fput"]["bucket_name"] == "mesh-artifacts"
    url = store.create_download_url(object_key="jobs/j/m.msh", expires_in=timedelta(seconds=900))
    assert url == "http://minio/sign" and calls["url"][2] == timedelta(seconds=900)














# artifact_uploader: ordering + no-orphan-duplicate
class _FakeStore:
    def __init__(self, fail=False):
        self.fail = fail; self.uploaded = []
    def upload_file(self, *, local_path, object_key, content_type=None, metadata=None):
        if self.fail:
            raise StorageError("boom")
        self.uploaded.append(object_key)
        return StoredObject(object_key=object_key, size_bytes=Path(local_path).stat().st_size,
                            content_type=content_type, checksum="ck")
    def delete_object(self, *, object_key):
        self.deleted = getattr(self, "deleted", [])
        self.deleted.append(object_key)


class _FakeArtifactRepo:
    def __init__(self): self.rows = []
    async def get_by_job(self, db, job_id): return list(self.rows)
    async def create(self, db, *, job_id, artifact_type, storage_key, size_bytes=0, checksum=None,
                     logical_key=None, delivery_attempt=0):
        self.rows.append(SimpleNamespace(artifact_type=artifact_type, storage_key=storage_key,
                                         size_bytes=size_bytes, checksum=checksum,
                                         logical_key=logical_key or artifact_type.value,
                                         delivery_attempt=delivery_attempt))
    async def deliver_artifact(self, db, *, job_id, logical_key, artifact_type, storage_key,
                               size_bytes, checksum, delivery_attempt, execution_generation=0):
        from meshpipeline.persistence.repositories.artifact_repository import DeliveryOutcome
        existing = next((r for r in self.rows if r.artifact_type == artifact_type), None)
        if existing is None:
            self.rows.append(SimpleNamespace(artifact_type=artifact_type, storage_key=storage_key,
                                             size_bytes=size_bytes, checksum=checksum,
                                             logical_key=logical_key, delivery_attempt=delivery_attempt))
            return DeliveryOutcome.created
        existing.storage_key, existing.size_bytes = storage_key, size_bytes
        existing.checksum, existing.delivery_attempt = checksum, delivery_attempt
        return DeliveryOutcome.updated


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def _ws_with_deliverable(tmp_path, *, mesh=True):
    ws = build_workspace(tmp_path, "gmsh")
    if mesh:
        (ws / "mesh.msh").write_bytes(b"surface")
    return ws


def test_a_required_upload_failure_raises_and_writes_no_row(monkeypatch, tmp_path):
    import meshpipeline.application.artifact_uploader as au
    repo = _FakeArtifactRepo()
    monkeypatch.setattr(au, "get_object_store", lambda: _FakeStore(fail=True))
    monkeypatch.setattr(au, "ArtifactRepository", lambda: repo)
    with pytest.raises(au.RequiredArtifactDeliveryError):
        _run(au.upload_job_artifacts(db=SimpleNamespace(flush=_afn), job_id="j",
                                     workspace=_ws_with_deliverable(tmp_path), engine="gmsh"))
    assert repo.rows == []       # required storage failed → raised, no DB row, no false success


def test_deterministic_key_updates_the_existing_row_no_duplicate(monkeypatch, tmp_path):
    import meshpipeline.application.artifact_uploader as au
    from meshpipeline.persistence.models import ArtifactType
    repo = _FakeArtifactRepo()
    # pre-existing row for the same artifact type (a prior attempt)
    repo.rows.append(SimpleNamespace(artifact_type=ArtifactType.mesh, storage_key="old",
                                     size_bytes=0, checksum=None))
    store = _FakeStore()
    monkeypatch.setattr(au, "get_object_store", lambda: store)
    monkeypatch.setattr(au, "ArtifactRepository", lambda: repo)
    db = SimpleNamespace(flush=_afn)
    report = _run(au.upload_job_artifacts(db=db, job_id="j", workspace=_ws_with_deliverable(tmp_path), engine="gmsh"))
    mesh_rows = [r for r in repo.rows if r.artifact_type == ArtifactType.mesh]
    assert len(mesh_rows) == 1                  # updated, not duplicated
    assert mesh_rows[0].storage_key == "jobs/j/mesh.msh" and mesh_rows[0].checksum == "ck"
    assert report.required_all_delivered()      # the bundle was delivered too


async def _afn(*a, **k):
    return None


# the contract coverage itself
def test_the_current_provider_sdks_are_installed_for_contract_coverage():
    import importlib

    for sdk, why in (("minio", "the local/OSS object store"),
                     ("google.cloud.storage", "the hosted object store"),
                     ("google.api_core", "GCS precondition/exception types")):
        try:
            importlib.import_module(sdk)
        except ImportError as exc:  # pragma: no cover - the failure IS the point
            raise AssertionError(
                f"{sdk!r} is not installed, so {why} has no contract coverage in this "
                f"environment. It is pinned in requirements/runtime.txt - install it rather than "
                f"skipping the tests ({exc})") from exc
