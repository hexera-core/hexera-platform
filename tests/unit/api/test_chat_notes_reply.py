# Responsibility: Verify the notes box's reply lists the notes as stored, before any brief exists (demo dry run, 2 Nov: a kept note answered with an empty list).
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import meshpipeline.agents.intake.message as msg
from meshpipeline.api.v1.chat import router as chat_router
from meshpipeline.contracts import user_notes as un

_app = FastAPI()
_app.include_router(chat_router, prefix="/api/v1/chat")
SID = uuid.UUID("dddd1111-2222-4222-b222-dddddddddddd")
NOTE = "Keep fillets at the arm roots and resolve the bolt holes for the caliper mount."


async def _post_note(session):
    async def _add(session_id, owner_id, text, **kw):
        return msg.NoteStatus.added, session

    with patch.object(msg, "add_console_note", _add), \
            patch("meshpipeline.persistence.repositories.session_repository.SessionRepository"):
        async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
            return await c.post("/api/v1/chat/notes", json={"session_id": str(SID), "text": NOTE})


async def test_a_note_added_before_the_summary_comes_back_in_the_reply():
    session = SimpleNamespace(id=SID, request_txt="", user_notes=[un.make(NOTE, source=un.SOURCE_CONSOLE)],
                              messages=[], intake_patches=[])
    resp = await _post_note(session)
    assert resp.status_code == 200
    body = resp.json()
    assert body["brief"] is None, "no summary yet, so no brief"
    assert [n["text"] for n in body["user_notes"]] == [NOTE], "the stored note was not in the reply"
    assert body["user_notes"][0]["source"] == un.SOURCE_CONSOLE
