# Responsibility: Verify the notes reader (engines/mesh_requirements) is shown the user's own pictures only when its model route declares vision (NOTE-16), and that without vision - today - its request is exactly as before.
# Boundaries: no model and no store: the model is a fake that keeps the request, the store a dict.
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from meshpipeline.contracts import user_notes as UN
from meshpipeline.contracts import user_pictures as UP
from meshpipeline.engines import mesh_requirements as MR

SID = "sess-1"
PIC = {"id": "a" * 16, "kind": "image", "name": "bore.png", "mime": "image/png"}


def _state(attach: bool = True) -> dict:
    note = UN.make("0.5 mm on the bearing bore", source=UN.SOURCE_CONSOLE, attachments=[PIC] if attach else [])
    return {"user_notes": UN.clean([note]), "session_id": SID}


def _store(monkeypatch):
    monkeypatch.setattr(UP, "_read", lambda key: b"PNG" if key == UP.attachment_key(SID, PIC["id"], "file") else b"")


def test_the_route_has_no_vision_today_and_the_request_is_unchanged(monkeypatch):
    _store(monkeypatch)
    assert MR.sees_pictures() is False
    plain = MR.messages_for(_state(), route="fea")
    assert isinstance(plain[1]["content"], str) and "base64" not in plain[1]["content"]
    assert plain == MR.messages_for(_state(), route="fea", vision=False)


def test_with_vision_the_picture_follows_the_words_and_without_one_nothing_changes(monkeypatch):
    _store(monkeypatch)
    seen = MR.messages_for(_state(), route="fea", vision=True)[1]["content"]
    assert [p["type"] for p in seen] == ["text", "text", "text", "image_url"]
    assert seen[1]["text"] == UP.TOLD and "[n1]" in seen[2]["text"]
    assert MR.messages_for(_state(attach=False), route="fea", vision=True) == MR.messages_for(
        _state(attach=False), route="fea")


def test_decide_sends_the_picture_on_a_vision_route_and_keeps_no_data_url(monkeypatch):
    from meshpipeline.contracts import execution_guard, model_inference
    _store(monkeypatch)
    sent = []

    async def model(messages, **_k):
        sent.append(messages)
        return SimpleNamespace(tool_calls=[], failure_marker="", assistant_text="")

    async def owner(_why):
        return None

    monkeypatch.setattr(MR, "sees_pictures", lambda: True)
    monkeypatch.setattr(model_inference, "call_builder_model", model)
    monkeypatch.setattr(execution_guard, "assert_still_owner", owner)
    out = asyncio.run(MR.decide(_state(), route="fea"))
    assert any(p.get("type") == "image_url" for p in sent[0][1]["content"])
    assert "base64" not in json.dumps(out, default=str)
