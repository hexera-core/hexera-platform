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
    monkeypatch.setattr(store, "_client", lambda **_: fake)
    f = tmp_path / "m.msh"; f.write_bytes(b"data")
    so = store.upload_file(local_path=f, object_key="jobs/j/m.msh", content_type="application/octet-stream")
    assert isinstance(so, StoredObject) and so.object_key == "jobs/j/m.msh" and so.size_bytes == 4
    assert calls["fput"]["bucket_name"] == "mesh-artifacts"
    url = store.create_download_url(object_key="jobs/j/m.msh", expires_in=timedelta(seconds=900))
    assert url == "http://minio/sign" and calls["url"][2] == timedelta(seconds=900)


# A presigned URL is signed FOR its host: the host is part of the SigV4 signature, so a URL signed
# with the address THIS PROCESS dials (compose: minio:9000) is one the browser it is handed to
# cannot resolve - and cannot be repaired by rewriting the host, because that invalidates the
# signature. The symptom is a download button that silently fails, which is how it reached a user.
# Which endpoint the signing client is built with is therefore behaviour, not plumbing.
def test_the_download_url_is_signed_for_the_address_the_browser_uses(monkeypatch):
    monkeypatch.setattr(provcfg, "MINIO_BUCKET", "mesh-artifacts")
    monkeypatch.setattr(provcfg, "MINIO_ENDPOINT", "minio:9000")             # what the container dials
    monkeypatch.setattr(provcfg, "MINIO_PUBLIC_ENDPOINT", "localhost:9000")  # what the browser dials
    from meshpipeline.adapters.object_storage.minio import MinioStore
    seen = {}
    store = MinioStore()

    def _client(*, endpoint=None):
        seen["endpoint"] = endpoint
        return SimpleNamespace(
            presigned_get_object=lambda b, k, expires=None: "http://localhost:9000/signed")

    monkeypatch.setattr(store, "_client", _client)
    store.create_download_url(object_key="jobs/j/m.msh", expires_in=timedelta(seconds=900))
    assert seen["endpoint"] == "localhost:9000", (
        "the signing client must be built with the PUBLIC endpoint; signing with "
        f"{seen['endpoint']} yields a URL no browser can fetch")


# minio-py resolves a bucket's region with a live GetBucketLocation request before it signs,
# unless it was given one. The signing client is deliberately built on an address THIS PROCESS may
# not be able to reach, so that lookup is not merely slow there - it fails, and takes the whole
# download endpoint down with a 500. The tests above could not catch it: they replace _client
# wholesale, so the real construction never ran. This one builds the real thing.
def test_the_client_carries_a_region_so_signing_never_makes_a_lookup_call(monkeypatch):
    monkeypatch.setattr(provcfg, "MINIO_REGION", "us-east-1")
    from meshpipeline.adapters.object_storage.minio import MinioStore
    built = MinioStore()._client(endpoint="localhost:9000")
    # _base_url.region short-circuits minio-py's _get_region, which is what suppresses the call
    assert built._base_url.region == "us-east-1", (
        "without a region the client issues GetBucketLocation before signing, on an endpoint "
        "that need not be reachable from here")


def test_the_client_uses_the_endpoint_it_is_handed(monkeypatch):
    # The fix has two halves - create_download_url ASKS for the public endpoint, and _client HONOURS
    # it - and the tests above stub _client out, so they pin only the first. Dropping the parameter
    # and always dialling MINIO_ENDPOINT would leave every one of them green while restoring the
    # exact bug: a URL signed for a host no browser can resolve.
    monkeypatch.setattr(provcfg, "MINIO_ENDPOINT", "minio:9000")
    monkeypatch.setattr(provcfg, "MINIO_REGION", "us-east-1")
    from meshpipeline.adapters.object_storage.minio import MinioStore
    store = MinioStore()
    assert "minio:9000" in store._client()._base_url._url.netloc
    handed = store._client(endpoint="storage.example.com:9000")
    assert "storage.example.com:9000" in handed._base_url._url.netloc, (
        "the endpoint argument must reach the client, or signing silently uses the internal host")


def test_both_the_internal_and_the_signing_client_agree_on_region(monkeypatch):
    monkeypatch.setattr(provcfg, "MINIO_REGION", "eu-west-2")
    from meshpipeline.adapters.object_storage.minio import MinioStore
    store = MinioStore()
    # one source of truth: a mismatch would sign downloads into a different credential scope than
    # uploads were stored under, which fails as SignatureDoesNotMatch only at download time
    assert store._client()._base_url.region == "eu-west-2"
    assert store._client(endpoint="localhost:9000")._base_url.region == "eu-west-2"


# Blank means "one address serves both" - the hosted case, and the behaviour that predates the
# split. It must resolve to MINIO_ENDPOINT, not to an empty host.
def test_a_blank_public_endpoint_falls_back_to_the_process_endpoint():
    import importlib
    import os
    prev_public = os.environ.pop("MINIO_PUBLIC_ENDPOINT", None)
    prev_endpoint = os.environ.get("MINIO_ENDPOINT")
    os.environ["MINIO_ENDPOINT"] = "storage.internal:9000"
    try:
        assert importlib.reload(provcfg).MINIO_PUBLIC_ENDPOINT == "storage.internal:9000"
    finally:
        if prev_endpoint is None:
            os.environ.pop("MINIO_ENDPOINT", None)
        else:
            os.environ["MINIO_ENDPOINT"] = prev_endpoint
        if prev_public is not None:
            os.environ["MINIO_PUBLIC_ENDPOINT"] = prev_public
        importlib.reload(provcfg)














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
