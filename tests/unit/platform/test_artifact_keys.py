# Responsibility: Verify the artifact keys have their exact wire format and hang off one per-job prefix.
from __future__ import annotations

import json

import pytest
from tests.engine_workspaces import build_workspace

from meshpipeline.artifact_keys import (
    input_key,
    mesh_artifact_key,
    output_key,
    result_key,
    result_key_beside,
)


# the exact wire format
def test_the_four_keys_have_their_exact_wire_format():
    job = "abc123def456"
    assert input_key(job) == "jobs/abc123def456/input.tar.gz"
    assert output_key(job) == "jobs/abc123def456/output.tar.gz"
    assert result_key(job) == "jobs/abc123def456/result.json"
    assert mesh_artifact_key(job) == "jobs/abc123def456/mesh.msh"


@pytest.mark.parametrize("job", ["j", "0", "abc123def456", "9f8e7d6c5b4a"])
def test_every_key_hangs_off_the_same_per_job_prefix(job):
    for key in (input_key(job), output_key(job), result_key(job), mesh_artifact_key(job)):
        assert key.startswith(f"jobs/{job}/"), key


# the cross-process coupling (the fragile part)
@pytest.mark.parametrize("job", ["j", "abc123def456", "deadbeef0000"])
def test_the_runner_derived_marker_equals_the_pipeline_built_marker(job):
    assert result_key_beside(output_key(job)) == result_key(job)


# behavioural: the mesh runner writes to those keys
class _FakeBlob:
    def __init__(self, sink, key): self._sink, self._key = sink, key
    def upload_from_string(self, data, content_type=None):
        self._sink[self._key] = (data, content_type)


class _FakeBucket:
    def __init__(self, sink): self._sink = sink
    def blob(self, key): return _FakeBlob(self._sink, key)


class _FakeGcsClient:
    def __init__(self, sink): self._sink = sink
    def bucket(self, name): return _FakeBucket(self._sink)


def test_upload_result_writes_the_output_tar_and_the_marker_beside_it(monkeypatch, tmp_path):
    ws = build_workspace(tmp_path, "gmsh")
    from google.cloud import storage

    import meshpipeline.adapters.mesh_execution.gcs_exchange as gx

    (ws / "mesh.msh").write_text("mesh")
    sink: dict = {}
    monkeypatch.setattr(storage, "Client", lambda *a, **k: _FakeGcsClient(sink))

    job = "abc123def456"
    output_uri = f"gs://mesh-bucket/{output_key(job)}"
    gx.upload_result(output_uri, str(tmp_path), {"rc": 0})

    written = set(sink)
    assert output_key(job) in written, f"the meshed tar was not written to the output key: {written}"
    assert result_key(job) in written, f"the completion marker is not beside the output: {written}"
    # the marker really is the completion signal the pipeline parses
    marker_bytes, ctype = sink[result_key(job)]
    assert json.loads(marker_bytes) == {"rc": 0} and ctype == "application/json"


# behavioural: the uploader publishes the mesh under its key
async def test_artifact_uploader_publishes_the_mesh_under_its_key(monkeypatch, tmp_path):
    ws = build_workspace(tmp_path, "gmsh")
    import meshpipeline.application.artifact_uploader as au
    from meshpipeline.contracts import object_storage

    (ws / "mesh.msh").write_text("surface")            # the OPTIONAL preview
    ws = build_workspace(tmp_path, "gmsh")   # a complete gmsh deliverable, per its own contract

    uploaded: dict = {}

    class _FakeStore:
        def upload_file(self, *, local_path, object_key, content_type):
            from pathlib import Path as _P
            uploaded[object_key] = content_type
            return type("SO", (), {"object_key": object_key,
                                   "size_bytes": _P(local_path).stat().st_size, "checksum": "x"})()
        def delete_object(self, *, object_key): pass

    class _FakeArtifactRepo:
        async def get_by_job(self, *a, **k): return []
        async def create(self, *a, **k): return None
        async def deliver_artifact(self, *a, **k):
            from meshpipeline.persistence.repositories.artifact_repository import DeliveryOutcome
            return DeliveryOutcome.created

    class _Db:
        async def flush(self): return None

    object_storage.set_object_store(_FakeStore())
    monkeypatch.setattr(au, "ArtifactRepository", lambda: _FakeArtifactRepo(), raising=False)

    import uuid
    job = uuid.UUID("abcdef12-0000-4000-8000-000000000000")
    report = await au.upload_job_artifacts(db=_Db(), job_id=job, workspace=ws, engine="gmsh")

    assert mesh_artifact_key(str(job)) in uploaded, (
        f"mesh not published under mesh_artifact_key: {set(uploaded)}")
    assert report.required_all_delivered()   # the gmsh bundle was delivered too
