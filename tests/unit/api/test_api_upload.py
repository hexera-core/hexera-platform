# Responsibility: Verify an upload is accepted on its magic bytes, and its filename is sanitized.
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from meshpipeline.api.v1.upload import router as upload_router

_app = FastAPI()
_app.include_router(upload_router, prefix="/api/v1/upload")

_VALID_STEP = b"ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION(('test'),'2;1');\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"

_SESSION_ID = uuid.UUID("12345678-1234-4234-a234-123456789abc")


@asynccontextmanager
async def _mock_get_db():
    db = AsyncMock()
    db.commit = AsyncMock()
    # The RESULT is a sync double on purpose: SQLAlchemy's Result methods are not awaitable, and a
    # bare AsyncMock hands back coroutines that the code under test would treat as rows. The
    # source-object cleanup intent is looked up through here, so this has to behave like a Result.
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result)
    yield db


def _make_svc_mock():
    svc = MagicMock()
    svc.check_quotas = AsyncMock()
    svc.create_session = AsyncMock(return_value=_SESSION_ID)
    return svc



async def test_wrong_extension_rejected():
    async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
        resp = await c.post(
            "/api/v1/upload/step-file",
            files={"file": ("model.obj", b"some data", "application/octet-stream")},
        )
    assert resp.status_code == 422
    detail = resp.json()["detail"].lower()
    assert ".step" in detail or "stp" in detail or "step" in detail



async def test_empty_file_rejected(tmp_path):
    svc_mock = _make_svc_mock()
    with (
        patch("meshpipeline.persistence.session.get_db", _mock_get_db),
        patch("meshpipeline.application.job_service.JobService", return_value=svc_mock),
        patch("meshpipeline.api.v1.upload._JOBS_DIR", tmp_path),
    ):
        async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
            resp = await c.post(
                "/api/v1/upload/step-file",
                files={"file": ("model.step", b"", "application/octet-stream")},
            )
    assert resp.status_code == 422
    assert "empty" in resp.json()["detail"].lower()



async def test_invalid_magic_bytes_rejected(tmp_path):
    svc_mock = _make_svc_mock()
    with (
        patch("meshpipeline.persistence.session.get_db", _mock_get_db),
        patch("meshpipeline.application.job_service.JobService", return_value=svc_mock),
        patch("meshpipeline.api.v1.upload._JOBS_DIR", tmp_path),
    ):
        async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
            resp = await c.post(
                "/api/v1/upload/step-file",
                files={"file": ("model.step", b"PK\x03\x04not a step file at all", "application/octet-stream")},
            )
    assert resp.status_code == 400
    assert "ISO-10303-21" in resp.json()["detail"]



async def test_valid_step_file_accepted(tmp_path):
    svc_mock = _make_svc_mock()

    with (
        patch("meshpipeline.persistence.session.get_db", _mock_get_db),
        patch("meshpipeline.application.job_service.JobService", return_value=svc_mock),
        patch("meshpipeline.api.v1.upload._JOBS_DIR", tmp_path),
    ):
        async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
            resp = await c.post(
                "/api/v1/upload/step-file",
                files={"file": ("wing.step", _VALID_STEP, "application/octet-stream")},
            )
    assert resp.status_code == 200
    data = resp.json()
    assert "session_id" in data
    assert "intake_greeting" in data
    assert data["intake_greeting"]



async def test_filename_with_special_chars_is_sanitized(tmp_path):
    svc_mock = _make_svc_mock()

    with (
        patch("meshpipeline.persistence.session.get_db", _mock_get_db),
        patch("meshpipeline.application.job_service.JobService", return_value=svc_mock),
        patch("meshpipeline.api.v1.upload._JOBS_DIR", tmp_path),
    ):
        async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
            resp = await c.post(
                "/api/v1/upload/step-file",
                files={"file": ("../../etc/passwd.step", _VALID_STEP, "application/octet-stream")},
            )
    assert resp.status_code in (200, 400, 422)
    if resp.status_code == 200:
        assert "/" not in resp.json().get("step_filename", "")
        assert "\\" not in resp.json().get("step_filename", "")
