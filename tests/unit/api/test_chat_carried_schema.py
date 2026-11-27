# Responsibility: Verify the chat message and notes requests carry attachments and picked faces (NOTE-16, NOTE-17): empty words allowed with either, limits held, and both passed through to the intake.
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

import meshpipeline.agents.intake.message as msg
from meshpipeline.api.schemas.chat import MAX_ATTACHMENTS, ChatMessageIn, ChatNoteIn
from meshpipeline.api.v1.chat import router as chat_router
from meshpipeline.contracts import user_notes as un

SID = uuid.UUID("eeee1111-2222-4222-b222-eeeeeeeeeeee")
FACE = {"id": "Face_12", "label": "Face 12 · round Ø20 mm"}


def test_plain_requests_are_as_before():
    assert ChatMessageIn(session_id=SID, content="hello").attachments == []
    assert ChatNoteIn(session_id=SID, text="2 mm").faces == []


@pytest.mark.parametrize("model,words", [(ChatMessageIn, "content"), (ChatNoteIn, "text")])
def test_empty_words_need_a_file_or_a_face(model, words):
    with pytest.raises(ValidationError):
        model(session_id=SID, **{words: "  "})
    assert model(session_id=SID, **{words: ""}, attachments=["a1"]).attachments == ["a1"]
    assert model(session_id=SID, faces=[FACE]).faces[0].id == "Face_12"


def test_limits():
    with pytest.raises(ValidationError):
        ChatMessageIn(session_id=SID, content="x", attachments=[f"a{i}" for i in range(MAX_ATTACHMENTS + 1)])
    with pytest.raises(ValidationError):
        ChatNoteIn(session_id=SID, text="x", faces=[{"id": "<script>", "label": ""}])
    assert ChatNoteIn(session_id=SID, text="x", faces=[{"id": "Part_2_Face_7"}, {"id": "Region_3"}]).faces


async def test_the_notes_route_passes_files_and_faces_through_and_returns_them():
    seen = {}
    note = un.make("", source=un.SOURCE_CONSOLE, attachments=[{"id": "a1", "name": "bore.png"}], faces=[FACE])
    session = SimpleNamespace(id=SID, request_txt="", user_notes=[note], messages=[], intake_patches=[])

    async def _add(session_id, owner_id, text, **kw):
        seen.update(kw, text=text)
        return msg.NoteStatus.added, session

    app = FastAPI()
    app.include_router(chat_router, prefix="/api/v1/chat")
    async def _verified(session_id, faces, owner_id, organization_id):   # NOTE-17's check: tests/unit/api/test_chat_face_verify.py
        return [f.model_dump() for f in faces or ()]

    with patch.object(msg, "add_console_note", _add), \
            patch("meshpipeline.api.v1.chat._picked_faces", _verified), \
            patch("meshpipeline.persistence.repositories.session_repository.SessionRepository"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/v1/chat/notes",
                                json={"session_id": str(SID), "attachments": ["a1"], "faces": [FACE]})
    assert resp.status_code == 200, resp.text
    assert seen["text"] == "" and seen["attachments"] == [{"id": "a1"}] and seen["faces"] == [FACE]
    (row,) = resp.json()["user_notes"]
    assert row["attachments"] == [{"id": "a1", "name": "bore.png"}] and row["faces"] == [FACE]


class _Repo:
    def __init__(self, session):
        self.session, self.notes = session, None

    async def get_for_owner(self, db, sid, owner, organization_id=""):
        return self.session

    async def set_user_notes(self, db, sid, notes):
        self.notes = notes


class _Db:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def commit(self):
        pass


AID = "0123456789abcdef"
INDEX = {AID: {"kind": "image", "name": "bore.png", "mime": "image/png", "bytes": 10,
               "object_key": f"sessions/{SID}/attachments/{AID}/file", "sha256": "x"}}


async def test_add_console_note_keeps_a_picture_only_note_as_its_kept_row():
    repo = _Repo(SimpleNamespace(job_id=None, attachments=INDEX))
    status, _ = await msg.add_console_note(SID, "o", "", session_repo=repo, db_factory=_Db,
                                           attachments=[{"id": AID}])
    assert status is msg.NoteStatus.added
    (note,) = repo.notes
    assert note["text"] == ""
    assert note["attachments"] == [{"id": AID, "kind": "image", "name": "bore.png", "mime": "image/png", "bytes": 10}]
    status, _ = await msg.add_console_note(SID, "o", "", session_repo=repo, db_factory=_Db,
                                           attachments=[{"id": "fedcba9876543210"}])
    assert status is msg.NoteStatus.empty, "an id this session does not hold is no attachment"
    status, _ = await msg.add_console_note(SID, "o", " ", session_repo=repo, db_factory=_Db)
    assert status is msg.NoteStatus.empty


def test_an_inbound_message_carries_its_rows():
    m = msg.InboundMessage(session_id=SID, owner_id="o", content="", attachments=({"id": "a1"},), faces=(FACE,))
    assert m.carried() == {"attachments": [{"id": "a1"}], "faces": [FACE]}
    assert msg.InboundMessage(session_id=SID, owner_id="o", content="hi").carried() == {}
