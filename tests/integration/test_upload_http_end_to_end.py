# Responsibility: Verify a real HTTP upload becomes durable state that survives the staging root being destroyed.
from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio

if not os.getenv("DATABASE_URL") or not os.getenv("MINIO_ENDPOINT"):
    pytest.skip("real PostgreSQL and MinIO endpoints are required",
                allow_module_level=True)

_API_KEY = "integration-api-key"
_USER_SECRET = "integration-user-secret"
_OWNER = "tenant-http"


def _payload(marker: str, size: int = 6144) -> bytes:
    body = f"ISO-10303-21;/* {marker} */\n".encode()
    return body + bytes((i * 13 + 7) % 251 for i in range(max(0, size - len(body))))


def _sig(user_id: str) -> str:
    return hmac.new(_USER_SECRET.encode(), user_id.encode(), hashlib.sha256).hexdigest()


def _headers() -> dict:
    return {"X-API-Key": _API_KEY, "X-User-Id": _OWNER, "X-User-Sig": _sig(_OWNER)}


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def api_server(tmp_path):
    import httpx

    staging = tmp_path / "api-staging"
    staging.mkdir()
    port = _free_port()
    env = {
        **os.environ,
        "MESH_API_KEY": _API_KEY,
        "USER_TOKEN_SECRET": _USER_SECRET,
        "JOBS_DIR": str(staging),
        "INTAKE_GREETING_ON_UPLOAD": "true",
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
    }
    # The COMPOSED entrypoint the image runs - meshpipeline.api.app is the bare router and
    # never calls the composition root, so an object store would never be configured.
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "meshpipeline.runtime.api_server:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 60
    while time.time() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"API process exited early:\n{proc.stdout.read()[-3000:]}")
        try:
            httpx.get(f"{base}/health", timeout=1.0)
            break
        except Exception:  # noqa: BLE001 - still binding
            time.sleep(0.3)
    else:
        proc.kill()
        pytest.fail("API process never became reachable")

    yield {"base": base, "pid": proc.pid, "staging": staging, "cwd": tmp_path}

    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


async def _upload(api, payload: bytes, filename="duct.step"):
    import httpx
    async with httpx.AsyncClient(base_url=api["base"], timeout=60.0) as c:
        return await c.post("/api/v1/upload/step-file", headers=_headers(),
                            files={"file": (filename, payload, "application/octet-stream")})


async def _row(session_id):
    from sqlalchemy import text

    from meshpipeline.persistence.session import dispose_engine, get_db
    async with get_db() as db:
        res = await db.execute(text(
            "select s.geometry_source_id, g.object_key, g.sha256, g.size_bytes, g.owner_id, "
            "       g.original_filename "
            "from chat_sessions s join geometry_sources g on g.id = s.geometry_source_id "
            "where s.id = :sid"), {"sid": uuid.UUID(session_id)})
        out = res.mappings().first()
    await dispose_engine()
    return dict(out) if out else None


# the upload itself

async def test_a_real_http_upload_becomes_durable_state(api_server, tmp_path):
    from meshpipeline.adapters.object_storage.factory import build_object_store
    payload = _payload("http-upload")

    resp = await _upload(api_server, payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    session_id = body["session_id"]

    # --- the database says what the API stored
    row = await _row(session_id)
    assert row is not None, "the session was not bound to a geometry source"
    assert row["owner_id"] == _OWNER
    assert row["sha256"] == hashlib.sha256(payload).hexdigest()
    assert row["size_bytes"] == len(payload)
    assert row["object_key"].startswith("sources/")
    assert row["original_filename"] == "duct.step"

    # --- the object store holds exactly those bytes
    store = build_object_store()
    assert store.exists(object_key=row["object_key"])
    fetched = tmp_path / "fetched.bin"
    store.download_file(object_key=row["object_key"], destination=fetched)
    assert fetched.read_bytes() == payload

    # --- no local path was persisted as identity anywhere on the row
    assert not any("/" in str(v) for k, v in row.items()
                   if k not in ("object_key",)), row


async def test_the_greeting_is_returned_once_and_replays_once(api_server):
    import httpx

    from meshpipeline.api.v1.upload import UPLOAD_ACKNOWLEDGEMENT

    resp = await _upload(api_server, _payload("greeting"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["intake_greeting"] == UPLOAD_ACKNOWLEDGEMENT

    async with httpx.AsyncClient(base_url=api_server["base"], timeout=30.0) as c:
        for _ in range(3):                       # a reconnecting client re-reads history
            hist = await c.get(f"/api/v1/chat/history/{body['session_id']}", headers=_headers())
            assert hist.status_code == 200, hist.text
            greetings = [m for m in hist.json()["messages"]
                         if m["content"] == UPLOAD_ACKNOWLEDGEMENT]
            assert len(greetings) == 1, greetings
            assert greetings[0]["role"] == "assistant"


async def test_public_http_text_leaks_no_storage_detail(api_server):
    import httpx
    resp = await _upload(api_server, _payload("privacy"))
    session_id = resp.json()["session_id"]
    row = await _row(session_id)

    async with httpx.AsyncClient(base_url=api_server["base"], timeout=30.0) as c:
        hist = await c.get(f"/api/v1/chat/history/{session_id}", headers=_headers())

    blob = (resp.text + hist.text).lower()
    for leak in (row["object_key"].lower(), row["sha256"].lower(), "sources/",
                 str(api_server["staging"]).lower(), "minio", "srcint-data",
                 "srcintadminsecret", "postgres", "amazonaws", "traceback"):
        assert leak not in blob, f"public HTTP text exposed {leak!r}"
    # the durable source id is internal - the session id is the client's handle
    assert str(row["geometry_source_id"]) not in blob


async def test_history_is_refused_for_another_tenant(api_server):
    import httpx
    resp = await _upload(api_server, _payload("tenant"))
    session_id = resp.json()["session_id"]
    other = "tenant-intruder"
    async with httpx.AsyncClient(base_url=api_server["base"], timeout=30.0) as c:
        hist = await c.get(f"/api/v1/chat/history/{session_id}",
                           headers={"X-API-Key": _API_KEY, "X-User-Id": other,
                                    "X-User-Sig": _sig(other)})
    assert hist.status_code == 404


# the machine is disposable

async def test_reconstruction_survives_the_api_staging_root_being_destroyed(api_server, tmp_path):
    payload = _payload("disposable", 9000)
    resp = await _upload(api_server, payload)
    session_id = resp.json()["session_id"]
    row = await _row(session_id)

    shutil.rmtree(api_server["staging"], ignore_errors=True)
    assert not api_server["staging"].exists()

    pipeline_root = tmp_path / "pipeline-root"
    pipeline_root.mkdir()
    script = """
import asyncio, hashlib, json, os, sys
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
        mg = await materialize_for_job(db, ref, interp, workspace=Path(sys.argv[2]),
                                       job_id="http")
    print(json.dumps({"pid": os.getpid(), "cwd": os.getcwd(), "path": str(mg.path),
                      "sha": hashlib.sha256(Path(mg.path).read_bytes()).hexdigest()}))
asyncio.run(main())
"""
    ref_payload = {
        "source_id": str(row["geometry_source_id"]), "owner_id": row["owner_id"],
        "object_key": row["object_key"], "sha256": row["sha256"],
        "size_bytes": row["size_bytes"], "original_filename": row["original_filename"],
        "suffix_hint": ".step"}
    # This upload is synthetic bytes, so nothing declared a unit and none was recorded - the
    # ordinary path for an STL, and for anything whose declaration is not evidence. A run is only
    # reconstructable once that question has been answered, so the confirmation a user would give
    # is recorded here before the geometry is rebuilt.
    interpretation_payload = await _confirm_unit(row)

    proc = subprocess.run([sys.executable, "-c", script, json.dumps(ref_payload),
                           str(pipeline_root), json.dumps(interpretation_payload)],
                          cwd=pipeline_root, capture_output=True, text=True, timeout=120,
                          env={**os.environ})
    assert proc.returncode == 0, proc.stderr[-2500:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    assert out["pid"] not in (os.getpid(), api_server["pid"])   # a third, distinct process
    assert Path(out["cwd"]).resolve() == pipeline_root.resolve()
    assert pipeline_root in Path(out["path"]).parents
    assert out["sha"] == hashlib.sha256(payload).hexdigest()


async def _confirm_unit(row, unit_name: str = "mm") -> dict:
    from meshpipeline.contracts.geometry_source import GeometryInterpretationRef
    from meshpipeline.contracts.geometry_units import LengthUnit, ResolutionBasis
    from meshpipeline.persistence.repositories.geometry_interpretation_repository import (
        GeometryInterpretationRepository,
    )
    from meshpipeline.persistence.session import get_db

    async with get_db() as db:
        recorded = await GeometryInterpretationRepository().record(
            db, owner_id=row["owner_id"], geometry_source_id=row["geometry_source_id"],
            unit=LengthUnit(unit_name), basis=ResolutionBasis.user_confirmed,
            evidence="confirmed in conversation")
        await db.commit()
    return GeometryInterpretationRef.from_domain(recorded).to_payload()
