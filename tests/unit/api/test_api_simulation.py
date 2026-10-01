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


#: Job 4f18812f's durable record, as stored: the Windsor body, delivered with every gate passed and
#: its review unfinished (the reviewer's model provider refused the first call).
_DELIVERED_UNREVIEWED = {
    "engine": "snappy", "job_id": "4f18812f-d05c-4749-9072-af59c54b3764", "status": "succeeded",
    "purpose": "external_cfd", "attempts": 1, "owner_id": "owner-1", "failed_gate": "",
    "attempts_max": 3, "finalized_at": "2026-09-30T22:32:19.046935+00:00",
    "outcome_code": "success", "failure_cause": "", "retry_skipped": False,
    "dimensionality": "3D", "failure_detail": "", "required_ready": True, "schema_version": 5,
    "delivered_types": ["mesh", "mesh_bundle", "viewer_data"], "missing_outputs": [],
    "executor_success": True, "failure_category": None, "review_execution": "failed_to_complete",
    "reviewer_verdict": None, "failure_next_step": "", "optional_warnings": [],
    "patch_contract_ok": True,
    "requirement_caveats": [{"kind": "review_inconclusive", "marker": "reviewer_non_transient",
                             "reruns": 0}],
    "approved_snapshot_id": "", "mesh_fidelity_source": "default",
    "effective_mesh_fidelity": "standard", "fidelity_policy_version": "3tier-v1",
    "requested_mesh_fidelity": None}


async def _served(monkeypatch, final_result, review):
    async def _vdata(*_a, **_k):
        return {"mesh_available": True, "review": review}
    monkeypatch.setattr(sim_module, "_viewer_data_or_empty", _vdata)
    job = _make_mock_job()
    job.final_result = final_result
    return (await _get(job)).json()


async def test_a_delivered_mesh_whose_review_did_not_finish_never_reads_fail(monkeypatch):
    # the worker's copy of that review said "FAIL" (a non-verdict used to be saved as one); the
    # durable record says no verdict was reached, and the status must say the same
    body = await _served(monkeypatch, _DELIVERED_UNREVIEWED,
                         {"verdict": "FAIL", "reasoning": "", "axis_findings": {}})
    assert body["status"] == "succeeded"
    assert body["reviewer_verdict"] is None
    assert body["reviewer_findings"] == [] and body["reviewer_reasoning"] == ""
    assert body["final_result"]["reviewer_verdict"] is None
    assert body["final_message"].startswith("Delivered with a stated caveat")
    assert "FAIL" not in body["final_message"]


async def test_a_retained_earlier_review_is_not_served_as_this_runs_judgement(monkeypatch):
    stale = {"verdict": "FAIL", "reasoning": "an earlier attempt's words",
             "axis_findings": [{"axis_key": "boundary_layers", "passed": False}]}
    body = await _served(monkeypatch, {**_DELIVERED_UNREVIEWED, "review_execution": "not_reached"},
                         stale)
    assert body["reviewer_verdict"] is None
    assert body["reviewer_findings"] == [] and body["reviewer_reasoning"] == ""


async def test_a_concluded_review_serves_the_durable_verdict(monkeypatch):
    passed = {**_DELIVERED_UNREVIEWED, "review_execution": "completed",
              "reviewer_verdict": "passed", "requirement_caveats": []}
    body = await _served(monkeypatch, passed, {"verdict": "PASS", "reasoning": "clean"})
    assert body["reviewer_verdict"] == "PASS" and body["reviewer_reasoning"] == "clean"
    failed = {**_DELIVERED_UNREVIEWED, "status": "failed", "review_execution": "completed",
              "reviewer_verdict": "failed", "requirement_caveats": []}
    body = await _served(monkeypatch, failed, {"verdict": "FAIL", "reasoning": "layers short"})
    assert body["reviewer_verdict"] == "FAIL" and body["reviewer_reasoning"] == "layers short"


def test_before_a_record_exists_only_a_real_verdict_is_served():
    from meshpipeline.api.v1.simulation import served_review
    assert served_review(None, {"verdict": "PASS"})["verdict"] == "PASS"
    assert served_review(None, {"verdict": None, "reasoning": ""})["verdict"] is None
    assert served_review(None, {"verdict": "maybe"})["verdict"] is None
    assert served_review(None, {}).get("verdict") is None


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
