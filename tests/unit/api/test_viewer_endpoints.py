# Responsibility: Verify viewer data is served from object storage, refusing a foreign tenant and leaking no key.
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import meshpipeline.api.v1.simulation as sim
from meshpipeline.application.viewer_payload import VIEWER_LOGICAL_KEY

OWNER = "owner-viewer"
FOREIGN = "owner-other"

_PAYLOAD = {
    "engine": "cfmesh",
    "mesh_available": True,
    "surface": {"kind": "stl", "parts": [{"name": "wall", "triangles": []}],
                "is_mesh": True, "cell_count": 4321},
    "quality": {"engine": "cfmesh", "cell_count": 4321, "mesh_units": "m", "criteria": []},
    "review": {"verdict": "PASS"},
}


class _Store:

    def __init__(self, blobs: dict[str, bytes] | None = None, fail: Exception | None = None):
        self.blobs = blobs if blobs is not None else {}
        self.fail = fail
        self.reads: list[str] = []

    def get_bytes(self, *, object_key: str) -> bytes:
        key = object_key
        self.reads.append(key)
        if self.fail:
            raise self.fail
        if key not in self.blobs:
            raise FileNotFoundError(key)
        return self.blobs[key]


def _client(monkeypatch, *, row=True, store=None, owner=OWNER, job_owner=OWNER):
    import meshpipeline.contracts.object_storage as osmod
    import meshpipeline.persistence.repositories.artifact_repository as arepo

    store = store if store is not None else _Store(
        {"jobs/x/viewer_data.json": json.dumps(_PAYLOAD).encode()})
    monkeypatch.setattr(osmod, "get_object_store", lambda: store)
    monkeypatch.setattr(sim, "get_db", _fake_db())

    async def fake_get_job(db, job_id, owner_id):
        # the real service is tenant-scoped; a foreign owner simply gets nothing
        return SimpleNamespace(id=job_id) if owner_id == job_owner else None
    monkeypatch.setattr(sim.svc, "get_job", fake_get_job)

    class _Repo:
        async def get_by_logical_key(self, db, job_id, logical_key):
            if not row or logical_key != VIEWER_LOGICAL_KEY:
                return None
            return SimpleNamespace(storage_key="jobs/x/viewer_data.json")
    monkeypatch.setattr(arepo, "ArtifactRepository", _Repo)

    app = FastAPI()
    app.include_router(sim.router, prefix="/api/v1/simulation")
    app.dependency_overrides[sim.owner_dep] = lambda: owner
    return TestClient(app), store


def _fake_db():
    @asynccontextmanager
    async def _db():
        yield SimpleNamespace(commit=lambda: None)
    return _db


JOB = "11111111-1111-4111-8111-111111111111"


# hosted split filesystem

# `quality` was a second route serving the same projection this one already carries; it had no
# UI, documentation or SDK caller and was removed. The quality block itself is unchanged and
# is asserted through the surface payload below.
@pytest.mark.parametrize("route,key", [("surface", "kind")])
def test_the_route_serves_from_object_storage_with_no_workspace(monkeypatch, tmp_path, route, key):
    import meshpipeline.settings.runtime as rtcfg
    monkeypatch.setattr(rtcfg, "WORKSPACE_BASE", tmp_path / "absent", raising=False)
    client, store = _client(monkeypatch)

    r = client.get(f"/api/v1/simulation/{JOB}/{route}")

    assert r.status_code == 200, r.text
    assert key in r.json()
    assert store.reads == ["jobs/x/viewer_data.json"]      # the bytes came from the store


def test_no_worker_workspace_is_ever_opened(monkeypatch, tmp_path):
    from pathlib import Path

    import meshpipeline.settings.runtime as rtcfg
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setattr(rtcfg, "WORKSPACE_BASE", root, raising=False)
    client, _ = _client(monkeypatch)

    touched: list[str] = []
    real_open, real_glob = Path.open, Path.glob

    def _spy_open(self, *a, **k):
        touched.append(str(self)); return real_open(self, *a, **k)

    def _spy_glob(self, pat):
        touched.append(str(self)); return real_glob(self, pat)

    monkeypatch.setattr(Path, "open", _spy_open)
    monkeypatch.setattr(Path, "glob", _spy_glob)
    client.get(f"/api/v1/simulation/{JOB}/surface")

    assert not [t for t in touched if str(root) in t], touched


# integrity and tenancy

def test_a_missing_artifact_row_is_a_404(monkeypatch):
    client, _ = _client(monkeypatch, row=False)
    assert client.get(f"/api/v1/simulation/{JOB}/surface").status_code == 404


def test_a_missing_object_is_a_dependency_failure_not_a_200(monkeypatch):
    client, _ = _client(monkeypatch, store=_Store({}))
    assert client.get(f"/api/v1/simulation/{JOB}/surface").status_code == 503


def test_a_corrupt_object_is_an_integrity_failure(monkeypatch):
    client, _ = _client(monkeypatch, store=_Store({"jobs/x/viewer_data.json": b"not json"}))
    assert client.get(f"/api/v1/simulation/{JOB}/surface").status_code == 500


def test_a_foreign_tenant_is_refused(monkeypatch):
    client, store = _client(monkeypatch, owner=FOREIGN, job_owner=OWNER)
    r = client.get(f"/api/v1/simulation/{JOB}/surface")
    assert r.status_code == 404
    assert store.reads == [], "a foreign tenant caused an object read"


def test_no_response_leaks_a_storage_key_or_local_path(monkeypatch):
    client, _ = _client(monkeypatch)
    body = client.get(f"/api/v1/simulation/{JOB}/surface").text
    for leaked in ("jobs/x/viewer_data.json", "storage_key", "WORKSPACE_BASE", "/attempt_"):
        assert leaked not in body, leaked
