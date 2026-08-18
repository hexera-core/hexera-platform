# Responsibility: Verify a required deliverable fails closed, an optional one does not, and a retry leaves no orphan.
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.engine_workspaces import build_workspace

import meshpipeline.application.artifact_uploader as au
from meshpipeline.contracts.object_storage import StorageError, StoredObject
from meshpipeline.persistence.models import ArtifactType
from meshpipeline.persistence.repositories.artifact_repository import DeliveryOutcome

# a controllable in-memory store + repo (real uploader, faked boundaries only)

class _Store:
    def __init__(self, *, fail_keys=(), fail_all=False, corrupt=False, delete_fails=False):
        self.fail_keys, self.fail_all, self.corrupt, self.delete_fails = \
            set(fail_keys), fail_all, corrupt, delete_fails
        self.uploaded: list[str] = []
        self.deleted: list[str] = []

    def upload_file(self, *, local_path, object_key, content_type=None, metadata=None):
        if self.fail_all or object_key in self.fail_keys:
            raise StorageError(f"boom {object_key}")
        self.uploaded.append(object_key)
        size = Path(local_path).stat().st_size
        # corrupt => report a size that does not match the local file (truncation)
        return StoredObject(object_key=object_key, size_bytes=(size + 1 if self.corrupt else size),
                            content_type=content_type, checksum=None)

    def delete_object(self, *, object_key):
        if self.delete_fails:
            raise StorageError("delete boom")
        self.deleted.append(object_key)


class _Repo:
    def __init__(self, *, create_fails=False):
        self.create_fails = create_fails
        self.rows: list = []

    async def get_by_job(self, db, job_id):
        return list(self.rows)

    async def create(self, db, *, job_id, artifact_type, storage_key, size_bytes=0, checksum=None,
                     logical_key=None, delivery_attempt=0):
        if self.create_fails:
            raise RuntimeError("db write boom")
        row = SimpleNamespace(artifact_type=artifact_type, storage_key=storage_key,
                              size_bytes=size_bytes, checksum=checksum,
                              logical_key=logical_key or artifact_type.value,
                              delivery_attempt=delivery_attempt)
        self.rows.append(row)
        return row

    async def deliver_artifact(self, db, *, job_id, logical_key, artifact_type, storage_key,
                               size_bytes, checksum, delivery_attempt, execution_generation=0):
        if self.create_fails:
            raise RuntimeError("db write boom")
        existing = next((r for r in self.rows if r.artifact_type == artifact_type), None)
        if existing is None:
            self.rows.append(SimpleNamespace(artifact_type=artifact_type, storage_key=storage_key,
                                             size_bytes=size_bytes, checksum=checksum,
                                             logical_key=logical_key,
                                             delivery_attempt=delivery_attempt,
                                             execution_generation=execution_generation))
            return DeliveryOutcome.created
        # (missing columns default to 0, exactly as the NOT NULL DEFAULT 0 columns do in the DB)
        if ((execution_generation, delivery_attempt)
                < (getattr(existing, "execution_generation", 0),
                   getattr(existing, "delivery_attempt", 0))):
            return DeliveryOutcome.superseded
        existing.storage_key, existing.size_bytes = storage_key, size_bytes
        existing.checksum, existing.delivery_attempt = checksum, delivery_attempt
        existing.execution_generation = execution_generation
        return DeliveryOutcome.updated


class _Db:
    async def flush(self):
        return None


def _run(coro):
    return asyncio.run(coro)


def _ws(tmp_path, *, mesh=True, bundle=True):
    if not bundle:
        ws = tmp_path / "ws-empty"; ws.mkdir(parents=True, exist_ok=True)
        if mesh:
            (ws / "mesh.msh").write_bytes(b"surface-preview")
        return ws
    ws = build_workspace(tmp_path, "gmsh")
    if mesh:
        (ws / "mesh.msh").write_bytes(b"surface-preview")
    return ws


def _deliver(tmp_path, store, repo, job_id=None):
    import meshpipeline.contracts.object_storage as osmod
    _prev = osmod._store
    osmod.set_object_store(store)
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(au, "ArtifactRepository", lambda: repo)
            return _run(au.upload_job_artifacts(db=_Db(), job_id=job_id or uuid.uuid4(),
                                                workspace=tmp_path, engine="gmsh"))
    finally:
        osmod.set_object_store(_prev)


# required vs optional (section 9)

def test_happy_path_delivers_required_and_optional(tmp_path):
    store, repo = _Store(), _Repo()
    report = _deliver(_ws(tmp_path), store, repo)
    assert sorted(report.required) == ["mesh_bundle", "viewer_data"]
    assert report.required_all_delivered()
    assert {r.artifact_type for r in repo.rows} == {
        ArtifactType.mesh, ArtifactType.mesh_bundle, ArtifactType.viewer_data}
    assert not report.optional_failures and not report.orphaned_objects


def test_required_bundle_upload_failure_raises(tmp_path):
    store = _Store(fail_keys={"jobs/{}"})  # placeholder; fail by suffix below
    # fail exactly the bundle key
    store = _Store()
    store.fail_all = False
    orig = store.upload_file
    def _fail_bundle(*, local_path, object_key, content_type=None, metadata=None):
        if object_key.endswith("gmsh_case.tar.gz"):
            raise StorageError("bundle upload boom")
        return orig(local_path=local_path, object_key=object_key, content_type=content_type)
    store.upload_file = _fail_bundle
    repo = _Repo()
    with pytest.raises(au.RequiredArtifactDeliveryError):
        _deliver(_ws(tmp_path), store, repo)
    # the required bundle has no row; a false success is impossible
    assert not any(r.artifact_type == ArtifactType.mesh_bundle for r in repo.rows)


def test_optional_preview_failure_does_not_fail_the_job(tmp_path):
    store = _Store(fail_keys={"jobs/x"})  # replaced below
    store = _Store()
    orig = store.upload_file
    def _fail_mesh(*, local_path, object_key, content_type=None, metadata=None):
        if object_key.endswith("mesh.msh"):
            raise StorageError("preview upload boom")
        return orig(local_path=local_path, object_key=object_key, content_type=content_type)
    store.upload_file = _fail_mesh
    repo = _Repo()
    report = _deliver(_ws(tmp_path), store, repo)
    assert report.required_all_delivered()                 # bundle delivered
    assert "mesh" in report.optional_failures              # preview quietly failed
    assert not any(r.artifact_type == ArtifactType.mesh for r in repo.rows)


def test_missing_required_deliverable_fails_closed(tmp_path):
    store, repo = _Store(), _Repo()
    with pytest.raises(au.RequiredArtifactDeliveryError):
        _deliver(_ws(tmp_path, bundle=False), store, repo)
    assert store.uploaded == [] and repo.rows == []        # nothing uploaded before failing closed


def test_missing_workspace_fails_closed(tmp_path):
    store, repo = _Store(), _Repo()
    with pytest.raises(au.RequiredArtifactDeliveryError):
        _deliver(tmp_path / "does-not-exist", store, repo)


# object/row partial states (section 6)

def test_row_write_failure_after_upload_raises_and_deletes_the_orphan(tmp_path):
    store, repo = _Store(), _Repo(create_fails=True)
    with pytest.raises(au.RequiredArtifactDeliveryError):
        _deliver(_ws(tmp_path, mesh=False), store, repo)   # only the required bundle
    # the object was uploaded, the row failed, so the orphan is best-effort deleted
    assert any(k.endswith((".tar.gz", ".json")) for k in store.uploaded)
    assert any(k.endswith((".tar.gz", ".json")) for k in store.deleted)
    assert repo.rows == []


def test_row_failure_with_undeletable_orphan_is_recorded(tmp_path):
    store, repo = _Store(delete_fails=True), _Repo(create_fails=True)
    try:
        _deliver(_ws(tmp_path, mesh=False), store, repo)
        raise AssertionError("expected RequiredArtifactDeliveryError")
    except au.RequiredArtifactDeliveryError as exc:
        assert exc.orphaned_objects, "a stored object with no row must be reconcilable"


# integrity (section 10)

def test_a_truncated_object_is_not_accepted(tmp_path):
    store, repo = _Store(corrupt=True), _Repo()   # store reports size+1 (truncation/mismatch)
    with pytest.raises(au.RequiredArtifactDeliveryError, match="integrity"):
        _deliver(_ws(tmp_path, mesh=False), store, repo)
    assert repo.rows == []
    assert any(k.endswith((".tar.gz", ".json")) for k in store.deleted)   # orphan cleaned


def test_checksum_mismatch_is_rejected(tmp_path):
    store, repo = _Store(), _Repo()
    orig = store.upload_file
    def _wrong_ck(*, local_path, object_key, content_type=None, metadata=None):
        so = orig(local_path=local_path, object_key=object_key, content_type=content_type)
        return StoredObject(object_key=so.object_key, size_bytes=so.size_bytes,
                            checksum="0" * 32)   # md5-shaped but wrong
    store.upload_file = _wrong_ck
    with pytest.raises(au.RequiredArtifactDeliveryError, match="integrity"):
        _deliver(_ws(tmp_path, mesh=False), store, repo)


def test_a_non_md5_etag_is_not_false_failed(tmp_path):
    store, repo = _Store(), _Repo()
    orig = store.upload_file
    def _multipart_etag(*, local_path, object_key, content_type=None, metadata=None):
        so = orig(local_path=local_path, object_key=object_key, content_type=content_type)
        return StoredObject(object_key=so.object_key, size_bytes=so.size_bytes,
                            checksum="d41d8cd98f00b204e9800998ecf8427e-3")  # etag with part count
    store.upload_file = _multipart_etag
    report = _deliver(_ws(tmp_path, mesh=False), store, repo)
    assert report.required_all_delivered()


# idempotent retry (section 7)

def test_retry_updates_the_existing_row_no_duplicate(tmp_path):
    store = _Store()
    repo = _Repo()
    repo.rows.append(SimpleNamespace(artifact_type=ArtifactType.mesh_bundle,
                                     storage_key="old", size_bytes=0, checksum=None))
    report = _deliver(_ws(tmp_path, mesh=False), store, repo)
    bundle_rows = [r for r in repo.rows if r.artifact_type == ArtifactType.mesh_bundle]
    assert len(bundle_rows) == 1                            # updated in place, not duplicated
    assert bundle_rows[0].storage_key.endswith("gmsh_case.tar.gz")
    assert report.required_all_delivered()


def test_deterministic_key_means_a_retry_overwrites_the_same_object(tmp_path):
    jid = uuid.uuid4()
    store, repo = _Store(), _Repo()
    _deliver(_ws(tmp_path, mesh=False), store, repo, job_id=jid)
    store2, repo2 = _Store(), _Repo()
    _deliver(_ws(tmp_path, mesh=False), store2, repo2, job_id=jid)
    assert store.uploaded == store2.uploaded              # same deterministic key both attempts


def test_the_bundle_is_reproducible_gzip_mtime_is_zero(tmp_path):
    import struct

    captured = {}

    class _CapStore(_Store):
        def upload_file(self, *, local_path, object_key, content_type=None, metadata=None):
            if object_key.endswith("gmsh_case.tar.gz"):
                captured["bytes"] = Path(local_path).read_bytes()
            return super().upload_file(local_path=local_path, object_key=object_key,
                                       content_type=content_type)

    _deliver(_ws(tmp_path, mesh=False), _CapStore(), _Repo())
    raw = captured["bytes"]
    assert raw[:2] == b"\x1f\x8b", "not a gzip stream"
    mtime = struct.unpack("<I", raw[4:8])[0]     # gzip header MTIME field
    assert mtime == 0, f"bundle gzip mtime must be 0 for reproducibility, got {mtime}"


# attempt-identity documentation accuracy (MUTATION TARGET)
