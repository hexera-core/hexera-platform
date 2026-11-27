# Responsibility: Verify a message or note sent with picked faces is checked against the session's face map before it is kept (NOTE-17): the map's own label and signature are stored, a face not on the map refuses the send in one plain line, and another owner's session is never read.
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import meshpipeline.agents.intake.message as msg
from meshpipeline.api.v1.chat import router as chat_router
from meshpipeline.application import face_pick

SID = uuid.UUID("eeee1111-2222-4222-b222-eeeeeeeeeee2")
FACE = {"id": "Face_12", "label": "Face 12 · round Ø20 mm"}
SIG = {"area_m2": 1e-4, "centroid_m": [0.0, 0.0, 0.0], "normal": None}


class _Db:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _repo(session):
    class _Repo:
        async def get_for_owner(self, db, sid, owner, organization_id=""):
            return session
    return _Repo


async def _post(path: str, body: dict, *, session, verify, add=None, accept=None):
    app = FastAPI()
    app.include_router(chat_router, prefix="/api/v1/chat")
    calls = {"verify": [], "add": [], "accept": []}

    def _verify(sid, faces):
        calls["verify"].append((sid, faces))
        return verify(faces)

    async def _add(session_id, owner_id, text, **kw):
        calls["add"].append(kw)
        return msg.NoteStatus.added, SimpleNamespace(id=SID, request_txt="", user_notes=[], messages=[])

    async def _accept(inbound, **kw):
        calls["accept"].append(inbound)
        return SimpleNamespace(status=msg.MessageStatus.not_found)

    with patch.object(face_pick, "verify", _verify), patch.object(msg, "add_console_note", add or _add), \
            patch.object(msg, "accept", accept or _accept), patch("meshpipeline.api.v1.chat.get_db", _Db), \
            patch("meshpipeline.persistence.repositories.session_repository.SessionRepository", _repo(session)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(path, json=body)
    return resp, calls


async def test_a_note_keeps_the_maps_own_label_and_signature():
    kept = [{"id": "Face_12", "label": "Face 12 · round Ø15 mm", "sig": SIG}]
    resp, calls = await _post("/api/v1/chat/notes", {"session_id": str(SID), "text": "2 mm here", "faces": [FACE]},
                              session=SimpleNamespace(job_id=None), verify=lambda faces: (kept, []))
    assert resp.status_code == 200, resp.text
    assert calls["verify"] == [(str(SID), [FACE])]
    assert calls["add"][0]["faces"] == kept               # the client's label is replaced by the map's


async def test_a_face_not_on_the_model_refuses_the_note_in_one_line():
    resp, calls = await _post("/api/v1/chat/notes", {"session_id": str(SID), "text": "2 mm here", "faces": [FACE]},
                              session=SimpleNamespace(job_id=None), verify=lambda faces: ([], [FACE["label"]]))
    assert resp.status_code == 422
    assert resp.json()["detail"] == "Face 12 · round Ø20 mm is not on the model. Click the face on the model again, then send."
    assert calls["add"] == []                              # nothing kept


async def test_a_message_with_a_face_not_on_the_model_is_refused_before_the_intake():
    resp, calls = await _post("/api/v1/chat/message", {"session_id": str(SID), "content": "2 mm here", "faces": [FACE]},
                              session=SimpleNamespace(job_id=None), verify=lambda faces: ([], [FACE["label"]]))
    assert resp.status_code == 422 and "not on the model" in resp.json()["detail"]
    assert calls["accept"] == []


async def test_a_message_carries_the_verified_faces():
    kept = [{"id": "Face_12", "label": "Face 12 · round Ø15 mm", "sig": SIG}]
    resp, calls = await _post("/api/v1/chat/message", {"session_id": str(SID), "content": "2 mm here", "faces": [FACE]},
                              session=SimpleNamespace(job_id=None), verify=lambda faces: (kept, []))
    assert resp.status_code == 404                         # the fake intake says not found; what it got is the point
    (inbound,) = calls["accept"]
    assert list(inbound.faces) == kept


async def test_another_owners_session_is_never_read():
    resp, calls = await _post("/api/v1/chat/notes", {"session_id": str(SID), "text": "x", "faces": [FACE]},
                              session=None, verify=lambda faces: ([], []))
    assert resp.status_code == 404 and calls["verify"] == [] and calls["add"] == []


async def test_plain_sends_never_read_the_face_map():
    resp, calls = await _post("/api/v1/chat/notes", {"session_id": str(SID), "text": "2 mm"},
                              session=None, verify=lambda faces: ([], []))
    assert resp.status_code == 200 and calls["verify"] == [] and calls["add"][0]["faces"] == []


def test_the_refusal_names_what_the_user_saw():
    assert face_pick.refused(["Face 3 · flat 40 × 60 mm"]).startswith("Face 3 · flat 40 × 60 mm is not on the model.")
    many = face_pick.refused([f"Face {k}" for k in range(1, 6)])
    assert many.startswith("Face 1, Face 2, Face 3 and 2 more are not on the model.")
