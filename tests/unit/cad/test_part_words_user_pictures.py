# Responsibility: Verify NOTE-16 at the parts reader (cad/part_words + contracts/user_pictures): the user's own pictures reach the model beside the note they came with, at most MAX_PICTURES (the newest), only when a note carries one, and no data URL is ever kept in what the reading stores.
# Boundaries: no model and no store: the model is a fake that keeps the request, the store a dict, the drawing skipped.
from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

from meshpipeline.cad import part_words as PW
from meshpipeline.contracts import user_notes as UN
from meshpipeline.contracts import user_pictures as UP

TABLE = [{"id": "Part_1", "name": "", "diag_mm": 120.0}]
SID = "sess-1"


def _pic(n: int) -> dict:
    return {"id": f"{n:016x}", "kind": "image", "name": f"shot{n}.png", "mime": "image/png"}


def _store(monkeypatch, ids) -> dict:
    blobs = {UP.attachment_key(SID, f"{n:016x}", "file"): f"PNG-{n}".encode() for n in ids}
    monkeypatch.setattr(UP, "_read", lambda key: blobs[key])
    return blobs


def _ask(monkeypatch, state) -> tuple[dict, list]:
    from meshpipeline.contracts import execution_guard, model_inference
    sent: list = []
    answer = {"phrases": [], "words": ["read"]}

    async def model(messages, tools, **_k):
        sent.append(messages)
        return SimpleNamespace(tool_calls=[SimpleNamespace(name=PW.TOOL, arguments=json.dumps(answer))],
                               failure_marker="")

    async def owner(_why):
        return None

    monkeypatch.setattr(model_inference, "call_reviewer_with_tools", model)
    monkeypatch.setattr(execution_guard, "assert_still_owner", owner)
    out = asyncio.run(PW.ask(None, TABLE, [], state, consumer="fea_part_sizes"))
    return out, sent[0][1]["content"]


def _decoded(part: dict) -> bytes:
    return base64.b64decode(part["image_url"]["url"].split(",", 1)[1])


def _state(*notes) -> dict:
    return {"user_notes": UN.clean(list(notes)), "session_id": SID}


def test_the_users_picture_reaches_the_parts_reader_beside_the_note_it_came_with(monkeypatch):
    _store(monkeypatch, [1])
    state = _state(UN.make("2 mm on the frame rail", source=UN.SOURCE_CONSOLE),
                   UN.make("this cell size on the bearing bore: 0.5 mm", source=UN.SOURCE_CHAT, turn=3,
                           attachments=[_pic(1)]))
    out, content = _ask(monkeypatch, state)
    texts = [p["text"] for p in content if p["type"] == "text"]
    assert texts.count(UP.TOLD) == 1
    images = [i for i, p in enumerate(content) if p["type"] == "image_url"]
    assert len(images) == 1 and _decoded(content[images[0]]) == b"PNG-1"
    beside = content[images[0] - 1]["text"]
    assert "[n2]" in beside and "shot1.png" in beside and "said in the chat, message 3" in beside
    # the reading as kept: never a data URL, never the bytes
    kept = json.dumps(out, default=str)
    assert "base64" not in kept and "data:image" not in kept and "UE5HLTE" not in kept
    assert out["status"] == "used"


def test_no_picture_no_picture_parts(monkeypatch):
    _store(monkeypatch, [])
    _out, content = _ask(monkeypatch, _state(UN.make("2 mm on the frame rail", source=UN.SOURCE_CONSOLE)))
    assert [p["type"] for p in content] == ["text"]
    assert UP.TOLD not in json.dumps(content)


def test_at_most_four_pictures_the_newest(monkeypatch):
    _store(monkeypatch, range(1, 7))
    state = _state(UN.make("old", source=UN.SOURCE_CONSOLE, attachments=[_pic(1), _pic(2)]),
                   UN.make("mid", source=UN.SOURCE_CONSOLE, attachments=[_pic(3)]),
                   UN.make("new", source=UN.SOURCE_CONSOLE, attachments=[_pic(4), _pic(5), _pic(6)]))
    _out, content = _ask(monkeypatch, state)
    shown = [_decoded(p) for p in content if p["type"] == "image_url"]
    assert len(shown) == UP.MAX_PICTURES == 4
    assert shown == [b"PNG-3", b"PNG-4", b"PNG-5", b"PNG-6"]          # the newest four, in the notes' order


def test_a_picture_that_cannot_be_read_or_has_no_session_is_left_out_and_the_reading_goes_on(monkeypatch):
    def broken(_key):
        raise OSError("store down")

    monkeypatch.setattr(UP, "_read", broken)
    note = UN.make("here", source=UN.SOURCE_CONSOLE, attachments=[_pic(1)])
    out, content = _ask(monkeypatch, _state(note))
    assert [p["type"] for p in content] == ["text"] and out["status"] == "used"
    _store(monkeypatch, [1])
    _out, content = _ask(monkeypatch, {"user_notes": UN.clean([note])})
    assert [p["type"] for p in content] == ["text"]


def test_a_file_that_is_not_a_picture_is_never_sent_as_one(monkeypatch):
    _store(monkeypatch, [1])
    pdf = {"id": f"{1:016x}", "kind": "text", "name": "spec.pdf", "mime": "application/pdf"}
    _out, content = _ask(monkeypatch, _state(UN.make("see spec", source=UN.SOURCE_CONSOLE, attachments=[pdf])))
    assert [p["type"] for p in content] == ["text"]
