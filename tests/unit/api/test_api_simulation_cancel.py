# Responsibility: Verify the cancel route answers each of the authority's outcomes with the right status, and carries the reason on the job's status.
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import meshpipeline.api.v1.simulation as sim_module
from meshpipeline.api.v1.simulation import router as simulation_router
from meshpipeline.application import job_cancel
from meshpipeline.persistence.models import JobStatus

_app = FastAPI()
_app.include_router(simulation_router, prefix="/api/v1/simulation")

_JOB_ID = uuid.UUID("aaaabbbb-1111-4111-b111-aaaaaaaaaaaa")
_URL = f"/api/v1/simulation/{_JOB_ID}/cancel"


def _client():
    return AsyncClient(transport=ASGITransport(app=_app), base_url="http://test")


def _outcome(result, status=JobStatus.cancelled, reason=None):
    return job_cancel.CancelOutcome(result, status, reason)


def _authority(outcome):
    return patch.object(job_cancel, "cancel_job", new=AsyncMock(return_value=outcome))


async def test_a_cancel_answers_200_with_the_jobs_new_status():
    with _authority(_outcome(job_cancel.CancelResult.cancelled, reason="wrong file")) as fn:
        async with _client() as c:
            resp = await c.post(_URL, json={"reason": "wrong file"})
    assert resp.status_code == 200
    assert resp.json() == {"job_id": str(_JOB_ID), "status": "cancelled",
                           "cancel_reason": "wrong file", "already_cancelled": False}
    kw = fn.await_args.kwargs
    assert fn.await_args.args == (_JOB_ID,)
    assert kw["reason"] == "wrong file" and "owner_id" in kw and "organization_id" in kw


async def test_a_cancel_without_a_body_is_accepted():
    with _authority(_outcome(job_cancel.CancelResult.cancelled)) as fn:
        async with _client() as c:
            resp = await c.post(_URL)
    assert resp.status_code == 200
    assert fn.await_args.kwargs["reason"] == ""


async def test_a_repeat_cancel_is_200_and_says_so():
    with _authority(_outcome(job_cancel.CancelResult.already_cancelled, reason="earlier")):
        async with _client() as c:
            resp = await c.post(_URL, json={})
    assert resp.status_code == 200
    assert resp.json()["already_cancelled"] is True and resp.json()["cancel_reason"] == "earlier"


async def test_a_finished_job_is_409_and_says_how_it_finished():
    with _authority(_outcome(job_cancel.CancelResult.already_finished, status=JobStatus.succeeded)):
        async with _client() as c:
            resp = await c.post(_URL, json={})
    assert resp.status_code == 409 and "succeeded" in resp.json()["detail"]


async def test_an_unknown_or_foreign_job_is_404():
    with _authority(_outcome(job_cancel.CancelResult.not_found, status=None)):
        async with _client() as c:
            resp = await c.post(_URL, json={})
    assert resp.status_code == 404


async def test_an_oversized_reason_is_refused_at_the_boundary():
    with _authority(_outcome(job_cancel.CancelResult.cancelled)) as fn:
        async with _client() as c:
            resp = await c.post(_URL, json={"reason": "x" * 501})
    assert resp.status_code == 422 and fn.await_count == 0


async def test_a_malformed_job_id_is_422():
    async with _client() as c:
        resp = await c.post("/api/v1/simulation/not-a-uuid/cancel", json={})
    assert resp.status_code == 422


async def test_the_status_response_carries_the_cancel_reason():
    @asynccontextmanager
    async def _db():
        yield AsyncMock()

    now = datetime.now(UTC)
    job = MagicMock()
    job.id = _JOB_ID
    job.status = "cancelled"
    job.current_attempt = 1
    job.created_at = job.updated_at = job.started_at = job.ended_at = now
    job.artifacts = []
    job.final_result = None
    job.cancel_reason = "wrong file"
    with patch.object(sim_module, "get_db", _db), \
            patch.object(sim_module.svc, "get_job", new=AsyncMock(return_value=job)):
        async with _client() as c:
            resp = await c.get(f"/api/v1/simulation/{_JOB_ID}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled" and resp.json()["cancel_reason"] == "wrong file"


# a cancelled run delivers nothing, whatever a worker stored before the cancel committed

def _cancelled_job_with_rows():
    now = datetime.now(UTC)
    art = MagicMock()
    art.artifact_type = "mesh_bundle"
    art.storage_key = "jobs/x/mesh.tar.gz"
    job = MagicMock()
    job.id = _JOB_ID
    job.status = JobStatus.cancelled
    job.current_attempt = 1
    job.created_at = job.updated_at = job.started_at = job.ended_at = now
    job.artifacts = [art]            # registered a moment before the cancel committed
    job.final_result = None
    job.cancel_reason = None
    job.workspace_purged = False
    return job


@asynccontextmanager
async def _fake_db():
    yield AsyncMock()


async def test_a_cancelled_job_lists_no_downloads_even_with_rows_registered():
    job = _cancelled_job_with_rows()
    signed = AsyncMock(side_effect=AssertionError("a download link was minted for a cancelled run"))
    with patch.object(sim_module, "get_db", _fake_db), \
            patch.object(sim_module.svc, "get_job", new=AsyncMock(return_value=job)), \
            patch.object(sim_module.svc, "signed_url", new=signed):
        async with _client() as c:
            resp = await c.get(f"/api/v1/simulation/{_JOB_ID}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "cancelled" and body["artifacts"] == []
    assert body["mesh_available"] is False
    signed.assert_not_awaited()


async def test_a_cancelled_job_serves_no_surface():
    job = _cancelled_job_with_rows()
    with patch.object(sim_module, "get_db", _fake_db), \
            patch.object(sim_module.svc, "get_job", new=AsyncMock(return_value=job)):
        async with _client() as c:
            surface = await c.get(f"/api/v1/simulation/{_JOB_ID}/surface")
            vtk = await c.get(f"/api/v1/simulation/{_JOB_ID}/surface.vtk")
    assert surface.status_code == 404 and "cancelled" in surface.json()["detail"]
    assert vtk.status_code == 404


async def test_a_cancelled_job_cannot_be_disputed():
    job = _cancelled_job_with_rows()
    with patch.object(sim_module, "get_db", _fake_db), \
            patch.object(sim_module.svc, "get_job", new=AsyncMock(return_value=job)):
        async with _client() as c:
            resp = await c.post(f"/api/v1/simulation/{_JOB_ID}/dispute",
                                json={"flags": [], "comment": "finer wake", "mode": "rebuild"})
    assert resp.status_code == 409
    assert "cancelled" in resp.json()["detail"], "the refusal says 'not finished' for a stopped run"
