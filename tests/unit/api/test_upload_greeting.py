# Responsibility: Verify the upload acknowledgement is one canonical message, tenant-scoped and never duplicated.
import uuid
from contextlib import ExitStack, asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import meshpipeline.api.v1.upload as upload_mod
from meshpipeline.api.v1.upload import UPLOAD_ACKNOWLEDGEMENT
from meshpipeline.api.v1.upload import router as upload_router

_app = FastAPI()
_app.include_router(upload_router, prefix="/api/v1/upload")

_VALID_STEP = (b"ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION(('t'),'2;1');\nENDSEC;\n"
               b"DATA;\nENDSEC;\nEND-ISO-10303-21;\n")
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


async def _upload(tmp_path, *, greeting_enabled: bool, spy=None):
    svc = MagicMock()
    svc.check_quotas = AsyncMock()
    svc.create_session = AsyncMock(return_value=_SESSION_ID)
    store = MagicMock()
    store.upload_file = MagicMock()
    with ExitStack() as stack:
        for p in (
            patch("meshpipeline.persistence.session.get_db", _mock_get_db),
            patch("meshpipeline.application.job_service.JobService", return_value=svc),
            patch("meshpipeline.contracts.object_storage.get_object_store", return_value=store),
            patch.object(upload_mod, "_JOBS_DIR", tmp_path),
            patch.object(upload_mod.icfg, "INTAKE_GREETING_ON_UPLOAD", greeting_enabled),
        ):
            stack.enter_context(p)
        if spy is not None:
            stack.enter_context(patch("meshpipeline.agents.intake.agent.node_intake", new=spy))
        async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
            return await c.post(
                "/api/v1/upload/step-file",
                files={"file": ("wing.step", _VALID_STEP, "application/octet-stream")})


# the flag's two behaviours

@pytest.mark.asyncio
async def test_enabled_returns_the_one_canonical_acknowledgement(tmp_path):
    resp = await _upload(tmp_path, greeting_enabled=True)
    assert resp.status_code == 200, resp.text
    assert resp.json()["intake_greeting"] == UPLOAD_ACKNOWLEDGEMENT


@pytest.mark.asyncio
async def test_disabled_returns_no_greeting_and_still_accepts_the_upload(tmp_path):
    resp = await _upload(tmp_path, greeting_enabled=False)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["intake_greeting"] == ""       # the flag is off; nothing is invented in its place
    assert body["session_id"] == str(_SESSION_ID)   # the upload itself is unaffected


# the execution boundary

@pytest.mark.asyncio
async def test_the_upload_path_runs_no_pipeline_node(tmp_path):
    spy = AsyncMock(return_value={})
    resp = await _upload(tmp_path, greeting_enabled=True, spy=spy)
    assert resp.status_code == 200, resp.text
    spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_geometry_handle_is_fabricated_on_upload(tmp_path):
    from meshpipeline.contracts.geometry_source import MaterializedGeometry
    made: list = []
    real = MaterializedGeometry.__post_init__

    def _record(self):
        made.append(self)
        return real(self)

    with patch.object(MaterializedGeometry, "__post_init__", _record):
        resp = await _upload(tmp_path, greeting_enabled=True)
    assert resp.status_code == 200, resp.text
    assert made == []


# what the sentence may say

@pytest.mark.asyncio
async def test_the_acknowledgement_leaks_no_internal_detail(tmp_path):
    greeting = (await _upload(tmp_path, greeting_enabled=True)).json()["intake_greeting"]
    low = greeting.lower()
    for leak in ("sources/", "bucket", "sha256", "s3", "minio", "gcs", "postgres", "redis",
                 "celery", "cloud run", "container", "worker", "/tmp", "traceback", "owner_id",
                 "source_id", "object_key"):
        assert leak not in low, f"the acknowledgement exposes {leak!r}"
    assert str(_SESSION_ID) not in greeting
    assert "/" not in greeting.replace("I'll", "")   # no filesystem or object paths


@pytest.mark.asyncio
async def test_the_acknowledgement_claims_nothing_that_has_not_happened(tmp_path):
    low = (await _upload(tmp_path, greeting_enabled=True)).json()["intake_greeting"].lower()
    for claim in ("watertight", "manifold", "topology", "units", "mm", "inch", "valid",
                  "verified", "inspected", "analysed", "analyzed", "converted", "any cad",
                  "all formats", "supported formats", "meshing has begun", "meshing started",
                  "generating", "recommend", "cfmesh", "snappy", "gmsh"):
        assert claim not in low, f"the acknowledgement claims {claim!r}"


# persistence and replay
# The canonical surface for a conversation message is ChatSession.messages, read back by
# GET /chat/history/{session_id}. It is deliberately NOT the WebSocket stream: that route is
# /{job_id}/stream, authenticated by a ticket bound to one job, and an upload creates only a
# session - there is no job to subscribe to until the user confirms. Publishing the greeting
# there would mean inventing a greeting-specific channel, so these tests pin the surface the
# product actually has.

class _SessionStore:

    def __init__(self, owner_id="owner-1"):
        self.row = SimpleNamespace(id=_SESSION_ID, owner_id=owner_id, messages=[],
                                   request_txt="", job_id=None)
        self.intake_events: list = []

    def repo(store):
        class _Repo:
            async def get_for_owner(self, _db, session_id, owner_id, *, organization_id=""):
                # scoped exactly as the real query is: a foreign owner finds nothing
                row = store.row if session_id == store.row.id else None
                return row if row is not None and row.owner_id == owner_id else None

            async def get_internal(self, _db, session_id):
                return store.row if session_id == store.row.id else None

            async def append_message(self, _db, session_id, role, content):
                store.row.messages = [*store.row.messages, {"role": role, "content": content}]

            async def append_intake_event(self, _db, session_id, event):
                store.intake_events.append(event)      # must stay empty for a deterministic greeting
        return _Repo


async def _upload_into(store, tmp_path, *, greeting_enabled=True):
    with patch("meshpipeline.persistence.repositories.session_repository.SessionRepository",
               store.repo()):
        return await _upload(tmp_path, greeting_enabled=greeting_enabled)


async def _history(store, owner_id="owner-1"):
    from meshpipeline.api.v1.chat import get_chat_history
    # `chat.py` now holds `session_repo` at module scope (hoisted so the collection route is
    # patchable too), so this patches that instance directly rather than the `SessionRepository`
    # constructor a call-time `import` used to pick up.
    with (patch("meshpipeline.api.v1.chat.session_repo", store.repo()()),
          patch("meshpipeline.persistence.session.get_db", _mock_get_db)):
        return await get_chat_history(_SESSION_ID, owner_id=owner_id, organization_id="")


def _greetings(history) -> list[str]:
    return [m["content"] for m in history["messages"]
            if m["content"] == UPLOAD_ACKNOWLEDGEMENT]


@pytest.mark.asyncio
async def test_enabled_persists_exactly_one_canonical_message(tmp_path):
    store = _SessionStore()
    resp = await _upload_into(store, tmp_path)
    assert resp.json()["intake_greeting"] == UPLOAD_ACKNOWLEDGEMENT
    assert store.row.messages == [{"role": "assistant", "content": UPLOAD_ACKNOWLEDGEMENT}]


@pytest.mark.asyncio
async def test_refresh_returns_the_same_greeting(tmp_path):
    store = _SessionStore()
    resp = await _upload_into(store, tmp_path)
    history = await _history(store)
    assert _greetings(history) == [resp.json()["intake_greeting"]]


@pytest.mark.asyncio
async def test_repeated_refresh_never_duplicates_the_transcript(tmp_path):
    store = _SessionStore()
    await _upload_into(store, tmp_path)
    for _ in range(4):
        assert len(_greetings(await _history(store))) == 1
    assert len(store.row.messages) == 1


@pytest.mark.asyncio
async def test_the_greeting_is_an_assistant_conversation_message(tmp_path):
    store = _SessionStore()
    await _upload_into(store, tmp_path)
    (msg,) = store.row.messages
    assert set(msg) == {"role", "content"}
    assert msg["role"] == "assistant"


@pytest.mark.asyncio
async def test_no_agent_or_training_record_is_fabricated(tmp_path):
    store = _SessionStore()
    await _upload_into(store, tmp_path)
    assert store.intake_events == []


@pytest.mark.asyncio
async def test_disabled_persists_nothing_and_replays_nothing(tmp_path):
    store = _SessionStore()
    resp = await _upload_into(store, tmp_path, greeting_enabled=False)
    assert resp.json()["intake_greeting"] == ""
    assert store.row.messages == []
    assert _greetings(await _history(store)) == []
    assert store.intake_events == []


@pytest.mark.asyncio
async def test_history_is_tenant_scoped(tmp_path):
    from fastapi import HTTPException
    store = _SessionStore(owner_id="owner-1")
    await _upload_into(store, tmp_path)
    with pytest.raises(HTTPException) as exc:
        await _history(store, owner_id="someone-else")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_the_replayed_greeting_leaks_nothing(tmp_path):
    store = _SessionStore()
    await _upload_into(store, tmp_path)
    (text,) = _greetings(await _history(store))
    low = text.lower()
    for leak in ("sources/", "bucket", "sha256", "minio", "gcs", "postgres", "redis", "celery",
                 "cloud run", "container", "worker", "/tmp", "traceback", "owner-1"):
        assert leak not in low
    assert str(_SESSION_ID) not in text
