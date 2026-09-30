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


async def _get(mock_job):
    with patch.object(sim_module, "get_db", _mock_get_db):
        with patch.object(sim_module.svc, "get_job", new=AsyncMock(return_value=mock_job)):
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
                return await c.get(f"/api/v1/simulation/{_JOB_ID}")


async def test_a_pending_job_carries_the_worker_wake_estimate(monkeypatch):
    import meshpipeline.settings.runtime as rtcfg
    monkeypatch.setattr(rtcfg, "WORKER_WAKE_MINUTES", 8)
    job = _make_mock_job()
    job.status = "pending"
    job.started_at = None
    job.ended_at = None
    resp = await _get(job)
    assert resp.status_code == 200
    assert resp.json()["worker_wake_minutes"] == 8


async def test_a_started_job_carries_no_wake_estimate(monkeypatch):
    import meshpipeline.settings.runtime as rtcfg
    monkeypatch.setattr(rtcfg, "WORKER_WAKE_MINUTES", 8)
    running = _make_mock_job()
    running.status = "running"
    assert (await _get(running)).json()["worker_wake_minutes"] is None
    assert (await _get(_make_mock_job())).json()["worker_wake_minutes"] is None   # succeeded


async def test_the_reviewers_reasoning_is_served_as_plain_engineering_text(monkeypatch):
    # the reviewer model's own words go on the result card; LaTeX in them is made plain there
    review = {"verdict": "FAIL",
              "reasoning": r"First cell at \(y^+ \approx 80\) on `wing_1`, outside \(30\text{–}300\)."}

    async def _vdata(*_a, **_k):
        return {"review": review}
    monkeypatch.setattr(sim_module, "_viewer_data_or_empty", _vdata)
    body = (await _get(_make_mock_job())).json()
    assert body["reviewer_reasoning"] == "First cell at y⁺ ≈ 80 on `wing_1`, outside 30–300."


def test_the_wake_estimate_is_pure_over_the_row_and_zero_hides_it(monkeypatch):
    import meshpipeline.settings.runtime as rtcfg
    from meshpipeline.api.v1.simulation import worker_wake_estimate
    from meshpipeline.persistence.models import JobStatus
    monkeypatch.setattr(rtcfg, "WORKER_WAKE_MINUTES", 8)
    def row(status, started):
        return type("J", (), {"status": status, "started_at": started})()
    assert worker_wake_estimate(row(JobStatus.pending, None)) == 8
    assert worker_wake_estimate(row(JobStatus.queued, None)) == 8
    assert worker_wake_estimate(row(JobStatus.pending, _NOW)) is None
    assert worker_wake_estimate(row(JobStatus.running, None)) is None
    assert worker_wake_estimate(row(JobStatus.failed, None)) is None
    monkeypatch.setattr(rtcfg, "WORKER_WAKE_MINUTES", 0)
    assert worker_wake_estimate(row(JobStatus.pending, None)) is None
