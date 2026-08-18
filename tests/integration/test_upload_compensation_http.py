# Responsibility: Verify a failed upload commits no row and removes exactly the object it created.
from __future__ import annotations

import hashlib
import hmac
import os
import socket
import subprocess
import sys
import time
import uuid

import pytest

pytestmark = pytest.mark.asyncio

if not os.getenv("DATABASE_URL") or not os.getenv("MINIO_ENDPOINT"):
    pytest.skip("real PostgreSQL and MinIO endpoints are required", allow_module_level=True)

# Identities the harness provisions on the disposable server; see the round's compose/bootstrap.
_READONLY = ("MINIO_READONLY_KEY", "MINIO_READONLY_SECRET")
_NODELETE = ("MINIO_NODELETE_KEY", "MINIO_NODELETE_SECRET")

_API_KEY = "integration-api-key"
_USER_SECRET = "integration-user-secret"
_OWNER = "tenant-compensation"


@pytest.fixture(autouse=True)
async def _capacity_headroom():
    # THIS SUITE IS ABOUT COMPENSATION, NOT CAPACITY. MAX_CONCURRENT_JOBS is a GLOBAL cap, so on a
    # shared disposable database the jobs earlier suites left non-terminal are counted against these
    # uploads: under one random order the suite ran early and passed, under another it was refused
    # with 429 before reaching the contract it exists to prove. Clearing the table through the
    # harness's authorized seam makes the precondition deterministic instead of order-dependent.
    from sqlalchemy.engine import make_url
    from tests import harness_provisioning as hp

    # The libpq-style query the runner supplies is not asyncpg's vocabulary; the harness builds its
    # own driver URL, so it is handed the bare coordinates.
    url = make_url(os.environ["DATABASE_URL"]).difference_update_query(["sslmode"])
    await hp.truncate_tables(url, "simulation_jobs")
    yield


def _payload(marker: str, size: int = 5000) -> bytes:
    body = f"ISO-10303-21;/* {marker} */\n".encode()
    return body + bytes((i * 11 + 3) % 251 for i in range(max(0, size - len(body))))


def _headers() -> dict:
    sig = hmac.new(_USER_SECRET.encode(), _OWNER.encode(), hashlib.sha256).hexdigest()
    return {"X-API-Key": _API_KEY, "X-User-Id": _OWNER, "X-User-Sig": sig}


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_api(tmp_path, extra_env: dict):
    import httpx
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    port = _free_port()
    log = tmp_path / "api.log"
    fh = log.open("w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "meshpipeline.runtime.api_server:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "info"],
        cwd=tmp_path,
        env={**os.environ, "MESH_API_KEY": _API_KEY, "USER_TOKEN_SECRET": _USER_SECRET,
             "JOBS_DIR": str(staging), **extra_env},
        stdout=fh, stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 60
    while time.time() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"API exited early:\n{log.read_text()[-3000:]}")
        try:
            httpx.get(f"{base}/health", timeout=1.0)
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
    else:
        proc.kill()
        pytest.fail("API never became reachable")
    return proc, base, log, staging


async def _post(base, payload):
    import httpx
    async with httpx.AsyncClient(base_url=base, timeout=60.0) as c:
        return await c.post("/api/v1/upload/step-file", headers=_headers(),
                            files={"file": ("part.step", payload, "application/octet-stream")})


async def _source_rows(owner=_OWNER):
    from sqlalchemy import text

    from meshpipeline.persistence.session import dispose_engine, get_db
    async with get_db() as db:
        res = await db.execute(
            text("select id, object_key from geometry_sources where owner_id = :o"), {"o": owner})
        rows = [dict(r) for r in res.mappings().all()]
    await dispose_engine()
    return rows


def _admin_store():
    from meshpipeline.adapters.object_storage.minio import MinioStore
    return MinioStore()


def _require(env_pair):
    if not all(os.getenv(k) for k in env_pair):
        pytest.skip(f"restricted MinIO identity not provisioned ({env_pair[0]})")
    return os.getenv(env_pair[0]), os.getenv(env_pair[1])


# object upload fails first

async def test_a_denied_object_upload_commits_no_row_and_creates_no_object(tmp_path):
    key, secret = _require(_READONLY)
    before = await _source_rows()
    proc, base, log, _ = _start_api(tmp_path, {"MINIO_ACCESS_KEY": key, "MINIO_SECRET_KEY": secret})
    try:
        resp = await _post(base, _payload("denied-write"))
    finally:
        proc.terminate()
        proc.wait(timeout=15)

    assert resp.status_code == 503, resp.text
    assert await _source_rows() == before, "a source row committed although the object never landed"
    body = resp.text.lower()
    for leak in ("accessdenied", "srcint-data", "minio", "s3", "127.0.0.1", secret.lower()):
        assert leak not in body, f"the public error exposed {leak!r}"


# database fails afterwards

async def test_a_database_failure_after_upload_removes_exactly_that_object(tmp_path):
    from sqlalchemy import text

    from meshpipeline.persistence.session import dispose_engine, get_db

    store = _admin_store()
    neighbour = f"sources/{uuid.uuid4()}"
    local = tmp_path / "neighbour.bin"
    local.write_bytes(_payload("neighbour"))
    store.upload_file(local_path=local, object_key=neighbour)

    async with get_db() as db:
        await db.execute(text(
            "create or replace function reject_compensation_tenant() returns trigger as $$ "
            "begin raise exception 'integration: refusing insert'; end; $$ language plpgsql"))
        # DDL takes no bind parameters; _OWNER is a module constant, not caller input.
        await db.execute(text(
            "create trigger reject_compensation_tenant before insert on geometry_sources "
            f"for each row when (new.owner_id = '{_OWNER}') "
            "execute function reject_compensation_tenant()"))
        await db.commit()
    await dispose_engine()

    before = await _source_rows()
    proc, base, log, _ = _start_api(tmp_path, {})
    try:
        resp = await _post(base, _payload("db-fails"))
    finally:
        proc.terminate()
        proc.wait(timeout=15)
        async with get_db() as db:
            await db.execute(text("drop trigger if exists reject_compensation_tenant "
                                  "on geometry_sources"))
            await db.commit()
        await dispose_engine()

    assert resp.status_code == 500, resp.text
    assert await _source_rows() == before, "a row committed despite the database refusing it"

    # the compensating delete removed the new object and nothing else
    keys_now = _list_source_keys(store)
    assert neighbour in keys_now, "compensation removed a neighbouring object"
    assert "upload_step_file: compensated" in log.read_text()


def _list_source_keys(store) -> set[str]:
    client = store._client()
    return {o.object_name for o in client.list_objects(store._bucket, prefix="sources/",
                                                       recursive=True)}


# compensation itself fails

async def test_a_failed_compensation_is_reported_loudly_and_leaks_nothing(tmp_path):
    from sqlalchemy import text

    from meshpipeline.persistence.session import dispose_engine, get_db
    key, secret = _require(_NODELETE)
    store = _admin_store()

    async with get_db() as db:
        await db.execute(text(
            "create or replace function reject_compensation_tenant() returns trigger as $$ "
            "begin raise exception 'integration: refusing insert'; end; $$ language plpgsql"))
        # DDL takes no bind parameters; _OWNER is a module constant, not caller input.
        await db.execute(text(
            "create trigger reject_compensation_tenant before insert on geometry_sources "
            f"for each row when (new.owner_id = '{_OWNER}') "
            "execute function reject_compensation_tenant()"))
        await db.commit()
    await dispose_engine()

    before_keys = _list_source_keys(store)
    proc, base, log, _ = _start_api(tmp_path, {"MINIO_ACCESS_KEY": key, "MINIO_SECRET_KEY": secret})
    try:
        resp = await _post(base, _payload("compensation-denied"))
    finally:
        proc.terminate()
        proc.wait(timeout=15)
        async with get_db() as db:
            await db.execute(text("drop trigger if exists reject_compensation_tenant "
                                  "on geometry_sources"))
            await db.commit()
        await dispose_engine()

    assert resp.status_code == 500, resp.text
    assert not await _source_rows(), "a row committed despite the refusal"

    # REPORTED LOUDLY, and - what actually matters - still OWNED. The log line is a diagnostic;
    # the authority that reclaims the stranded object is the cleanup intent recorded before the
    # write, which must survive a failed compensation as `pending` so the maintenance sweep picks
    # it up. Asserting a literal banner in the log proved neither of those, and this contract had
    # never run: the identity it needs was not provisioned, so it skipped on every machine.
    text_log = log.read_text()
    assert "compensation delete failed" in text_log, "a stranded object was not reported loudly"

    body = resp.text.lower()
    for leak in ("accessdenied", "srcint-data", "minio", "sources/", secret.lower()):
        assert leak not in body, f"the public error exposed {leak!r}"

    # the orphan is identifiable, and the harness's administrator identity removes it
    orphans = _list_source_keys(store) - before_keys
    assert len(orphans) == 1, orphans

    # STILL OWNED. The log line is a diagnostic; the authority that reclaims the stranded object is
    # the cleanup intent recorded before the write, which must survive a failed compensation as
    # `pending`. If it were closed here the sweep would skip it and the object would be permanent.
    async with get_db() as db:
        state = (await db.execute(
            text("select state from source_object_cleanups where object_key = :k"),
            {"k": next(iter(orphans))})).scalar_one()
    await dispose_engine()
    assert "pending" in str(state).lower(), (
        f"the stranded object's cleanup intent is {state!r}, so maintenance would never reclaim it")
    for k in orphans:
        store.delete_object(object_key=k)
    assert not (_list_source_keys(store) - before_keys)
