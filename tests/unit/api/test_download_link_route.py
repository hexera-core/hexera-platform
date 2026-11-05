# Responsibility: Verify a Download click gets a NEWLY signed link for the caller's own deliverable,
#                 with a life that scales with the file, and nothing for a row the job read hides.
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import meshpipeline.api.v1.simulation as sim_module
import meshpipeline.settings.providers as provcfg
from meshpipeline.api.v1.simulation import router as simulation_router
from meshpipeline.persistence.models import ArtifactType, JobStatus

_app = FastAPI()
_app.include_router(simulation_router, prefix="/api/v1/simulation")

_JOB_ID = uuid.UUID("aaaabbbb-2222-4222-b222-aaaaaaaaaaaa")
_DECK_ID = uuid.UUID("cccccccc-3333-4333-b333-cccccccccccc")
_VIEWER_ID = uuid.UUID("dddddddd-4444-4444-b444-dddddddddddd")


def _client():
    return AsyncClient(transport=ASGITransport(app=_app), base_url="http://test")


def _url(artifact_id=_DECK_ID):
    return f"/api/v1/simulation/{_JOB_ID}/artifacts/{artifact_id}/download"


@asynccontextmanager
async def _fake_db():
    yield AsyncMock()


def _artifact(aid, kind, key, size):
    a = MagicMock()
    a.id, a.artifact_type, a.storage_key, a.size_bytes = aid, kind, key, size
    return a


def _job(status=JobStatus.succeeded, deck_size=900_000_000):
    job = MagicMock()
    job.id = _JOB_ID
    job.status = status
    job.created_at = job.updated_at = datetime.now(UTC)
    job.artifacts = [
        _artifact(_DECK_ID, ArtifactType.mesh_bundle, "jobs/x/fea_deck.inp", deck_size),
        _artifact(_VIEWER_ID, ArtifactType.viewer_data, "jobs/x/viewer.json", 1_000),
    ]
    return job


def _store(url="https://store/signed"):
    store = MagicMock()
    store.create_download_url.return_value = url
    return store


async def _get(job, store, artifact_id=_DECK_ID):
    with patch.object(sim_module, "get_db", _fake_db), \
            patch.object(sim_module.svc, "get_job", new=AsyncMock(return_value=job)) as get_job, \
            patch("meshpipeline.contracts.object_storage.get_object_store", return_value=store), \
            patch.object(provcfg, "MINIO_SIGNED_URL_TTL", 900):
        async with _client() as c:
            resp = await c.get(_url(artifact_id))
    return resp, get_job


async def test_a_click_gets_a_fresh_link_sized_for_a_large_fea_deck():
    store = _store()
    resp, get_job = await _get(_job(), store)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["download_url"] == "https://store/signed"
    assert body["artifact_id"] == str(_DECK_ID) and body["size_bytes"] == 900_000_000
    # 900 MB at 1 Mbit/s is two hours: a slow or resumed download is never refused half-way
    assert body["expires_in_seconds"] == 7200
    kw = store.create_download_url.call_args.kwargs
    assert kw["object_key"] == "jobs/x/fea_deck.inp"
    assert kw["expires_in"].total_seconds() == 7200
    assert kw["download_name"] == "fea_deck.inp"
    # tenant-scoped: the job is read for the caller, never by id alone
    assert "organization_id" in get_job.await_args.kwargs


async def test_every_click_signs_anew_never_from_a_cache():
    store = _store()
    await _get(_job(), store)
    await _get(_job(), store)
    assert store.create_download_url.call_count == 2


async def test_a_small_file_keeps_the_short_life():
    resp, _ = await _get(_job(deck_size=2_000_000), _store())
    assert resp.json()["expires_in_seconds"] == 900


async def test_an_unrecorded_size_is_measured_in_the_store():
    store = _store()
    store.stat_object.return_value = MagicMock(size_bytes=450_000_000)
    resp, _ = await _get(_job(deck_size=0), store)
    assert resp.json()["expires_in_seconds"] == 3600
    store.stat_object.assert_called_once()


async def test_another_tenants_job_is_not_found():
    store = _store()
    resp, _ = await _get(None, store)
    assert resp.status_code == 404
    store.create_download_url.assert_not_called()


async def test_an_unknown_artifact_is_not_found():
    store = _store()
    resp, _ = await _get(_job(), store, artifact_id=uuid.uuid4())
    assert resp.status_code == 404
    store.create_download_url.assert_not_called()


async def test_the_viewer_payload_is_not_a_download():
    store = _store()
    resp, _ = await _get(_job(), store, artifact_id=_VIEWER_ID)
    assert resp.status_code == 404
    store.create_download_url.assert_not_called()


async def test_a_cancelled_run_hands_out_no_link():
    store = _store()
    resp, _ = await _get(_job(status=JobStatus.cancelled), store)
    assert resp.status_code == 404
    store.create_download_url.assert_not_called()
