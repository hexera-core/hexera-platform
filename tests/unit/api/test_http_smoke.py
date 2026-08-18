# Responsibility: Verify liveness, readiness, the canonical UI redirect and a static asset all answer as declared.
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import meshpipeline.api.v1.upload as upload_mod
from meshpipeline.api.app import app, set_readiness_probe

# A minimally valid ASCII STL: one triangle. Enough that upload accepts it (non-empty, allowed
# suffix) without needing a licensed CAD file or any binary fixture.
_MINIMAL_STL = (
    b"solid part\n"
    b"  facet normal 0 0 1\n"
    b"    outer loop\n"
    b"      vertex 0 0 0\n"
    b"      vertex 1 0 0\n"
    b"      vertex 0 1 0\n"
    b"    endloop\n"
    b"  endfacet\n"
    b"endsolid part\n"
)


@pytest.fixture
def client() -> TestClient:
    # No `with`: constructing the client does not run the lifespan (which needs a DB).
    return TestClient(app)


def test_health_reports_liveness(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_readyz_reflects_the_injected_probe(client):
    async def _healthy():
        return True, {"status": "ready", "checks": {"object_store": "ok"}, "circuits": {}}

    set_readiness_probe(_healthy)
    try:
        r = client.get("/readyz")
        assert r.status_code == 200
        assert r.json()["status"] == "ready"
    finally:
        set_readiness_probe(None)


def test_root_redirects_to_the_canonical_ui_path(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/ui"


def test_ui_serves_the_frontend_document(client):
    r = client.get("/ui")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "<!doctype html" in r.text[:200].lower()


def test_a_static_asset_is_served_with_revalidation(client):
    import re as _re
    page = client.get("/ui")
    assert page.status_code == 200
    asset = _re.search(r'(?:href|src)="(/static/[^"]+)"', page.text)
    assert asset, "the page references no static asset"
    r = client.get(asset.group(1))
    assert r.status_code == 200
    assert "no-cache" in r.headers.get("cache-control", "")


def test_minimal_geometry_submission_is_accepted(client, tmp_path):
    session_id = uuid.uuid4()

    @asynccontextmanager
    async def _fake_db():
        db = AsyncMock()
        # A sync Result double: SQLAlchemy's Result methods are not awaitable, and a bare AsyncMock
        # returns coroutines the code under test would treat as rows. The source-object cleanup
        # intent is looked up through this session.
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=result)
        yield db

    svc = MagicMock()
    svc.check_quotas = AsyncMock(return_value=None)
    svc.create_session = AsyncMock(return_value=session_id)

    repo = MagicMock()
    repo.append_message = AsyncMock()

    store = MagicMock()
    uploaded: dict = {}
    store.upload_file = MagicMock(
        side_effect=lambda local_path, object_key: uploaded.update(
            {"key": object_key, "bytes": Path(local_path).read_bytes()}))

    with (
        patch("meshpipeline.persistence.session.get_db", _fake_db),
        patch("meshpipeline.application.job_service.JobService", return_value=svc),
        patch("meshpipeline.persistence.repositories.session_repository.SessionRepository",
              return_value=repo),
        patch("meshpipeline.contracts.object_storage.get_object_store", return_value=store),
        patch.object(upload_mod, "_JOBS_DIR", tmp_path),
        patch.object(upload_mod.icfg, "INTAKE_GREETING_ON_UPLOAD", False),
    ):
        r = client.post(
            "/api/v1/upload/step-file",
            files={"file": ("part.stl", _MINIMAL_STL, "application/octet-stream")},
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["session_id"] == str(session_id)
    assert body["step_filename"].endswith(".stl")
    assert body["intake_greeting"] == ""        # the greeting is switched off for this request
    # The bytes reached DURABLE storage - that, not a local file, is what an upload produces now.
    assert uploaded["bytes"] == _MINIMAL_STL
    assert uploaded["key"].startswith("sources/")
    # The staging buffer is deliberately gone: nothing may later mistake it for the source.
    assert not (tmp_path / str(session_id) / "input.stl").exists()
