# Responsibility: Verify the simulation route answers 404 for an unknown job and 422 for a malformed identifier.
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import meshpipeline.api.v1.simulation as sim_module
from meshpipeline.api.v1.simulation import router as simulation_router

_app = FastAPI()
_app.include_router(simulation_router, prefix="/api/v1/simulation")

_JOB_ID = uuid.UUID("aaaabbbb-1111-4111-b111-aaaaaaaaaaaa")
_NOW = datetime.now(UTC)


@asynccontextmanager
async def _mock_get_db():
    yield AsyncMock()


def _make_mock_job(artifacts=None):
    job = MagicMock()
    job.id = _JOB_ID
    job.status = "succeeded"
    job.current_attempt = 1
    job.created_at = _NOW
    job.updated_at = _NOW
    job.started_at = _NOW
    job.ended_at = _NOW
    job.artifacts = artifacts or []
    job.final_result = None   # no persisted verdict in this fixture
    return job



async def test_unknown_job_returns_404():
    with patch.object(sim_module, "get_db", _mock_get_db):
        with patch.object(sim_module.svc, "get_job", new=AsyncMock(return_value=None)):
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                resp = await c.get(f"/api/v1/simulation/{_JOB_ID}")
    assert resp.status_code == 404



async def test_existing_job_returns_200():
    mock_job = _make_mock_job()
    with patch.object(sim_module, "get_db", _mock_get_db):
        with patch.object(sim_module.svc, "get_job", new=AsyncMock(return_value=mock_job)):
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                resp = await c.get(f"/api/v1/simulation/{_JOB_ID}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == str(_JOB_ID)
    assert data["status"] == "succeeded"
    assert data["artifacts"] == []
    assert data["current_attempt"] == 1



async def test_malformed_job_id_returns_422():
    async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
        resp = await c.get("/api/v1/simulation/not-a-uuid")
    assert resp.status_code == 422
