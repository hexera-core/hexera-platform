# Responsibility: Verify the user's pictures reach the intake model only on the newest turn, at most four, only when
#   the route declares vision (else the one-line fallback is asked for), and that no picture's bytes are ever kept.
# Boundaries: pure functions with a fake loader; no model, no store, no keys.
from __future__ import annotations

import pytest

from meshpipeline.agents.intake import agent as A
from meshpipeline.agents.intake import turn as T
from meshpipeline.application import chat_attachments as ca


def _pic(i: int, name: str = "") -> dict:
    return {"id": f"{i:016x}", "kind": "image", "name": name or f"p{i}.png", "mime": "image/png"}


def _llm(state):
    return A._build_llm_messages("SYSTEM", state)


def _load(row):
    return "data:image/png;base64,AAA" + row["id"][-2:]


def test_only_the_newest_turn_shows_its_pictures_and_at_most_four():
    state = [{"role": "user", "content": "old ask", "attachments": [_pic(1, "old.png")]},
             {"role": "assistant", "content": "ok"},
             {"role": "user", "content": "this bore at 2 mm", "attachments": [_pic(i) for i in range(2, 8)]
              + [{"id": "f" * 16, "kind": "text", "name": "spec.pdf"}]}]
    out = A._with_pictures(_llm(state), state, vision=True, load=_load)
    assert out[1]["content"] == "old ask\n[the user attached a picture earlier: old.png]"
    parts = out[3]["content"]
    assert parts[0] == {"type": "text", "text": "this bore at 2 mm"}
    urls = [p["image_url"]["url"] for p in parts[1:]]
    assert len(urls) == A.MAX_TURN_PICTURES == 4 and all(u.startswith("data:image/png;base64,") for u in urls)


def test_without_vision_the_model_is_told_to_say_it_could_not_look():
    state = [{"role": "user", "content": "", "attachments": [_pic(1, "bore.png")]}]
    calls = []
    out = A._with_pictures(_llm(state), state, vision=False, load=lambda r: calls.append(r) or "x")
    assert calls == [] and isinstance(out[1]["content"], str)
    assert "bore.png" in out[1]["content"] and A.NO_VISION_LINE in out[1]["content"]
    assert A.NO_VISION_LINE == "I could not look at the picture here; it is kept with your notes."


def test_the_intake_route_has_no_vision_today_so_the_fallback_runs():
    assert A._intake_sees_pictures() is False


def test_a_picture_that_cannot_be_read_is_named_and_a_turn_without_pictures_is_untouched():
    state = [{"role": "user", "content": "see", "attachments": [_pic(1)]}]
    out = A._with_pictures(_llm(state), state, vision=True, load=lambda r: None)
    assert out[1]["content"] == "see\n[the user attached a picture that could not be read now: p1.png]"
    plain = [{"role": "user", "content": "hello"}]
    msgs = _llm(plain)
    assert A._with_pictures(msgs, plain, vision=True, load=_load) is msgs


def test_a_nudge_after_the_picture_turn_is_left_alone():
    state = [{"role": "user", "content": "see", "attachments": [_pic(1)]}]
    msgs = _llm(state) + [{"role": "user", "content": T.SYNTHETIC_NUDGES[0]}]
    out = A._with_pictures(msgs, state, vision=True, load=_load)
    assert isinstance(out[1]["content"], list) and out[2]["content"] == T.SYNTHETIC_NUDGES[0]


def test_the_transcript_never_keeps_a_pictures_bytes():
    state = [{"role": "user", "content": "this bore", "attachments": [_pic(1)]}]
    out = A._with_pictures(_llm(state), state, vision=True, load=_load)
    kept = T.serialise_transcript(out, "Got it.")
    assert kept[0] == {"role": "user", "content": "this bore\n[picture]"}
    assert "base64" not in repr(kept)


def test_the_prompt_block_names_files_pictures_and_faces():
    text = A._block_attachments_and_faces()
    assert "own words" in text and "type it" in text and "faces" in text and "those faces only" in text
    assert "attachments_and_faces" in [b[0] for b in A.INTAKE_PROMPT_BLOCKS]


def test_data_url_reads_the_stored_picture_and_refuses_what_is_not_one(monkeypatch):
    seen = []
    monkeypatch.setattr(ca, "get_bytes", lambda key: seen.append(key) or b"\x89PNG")
    row = _pic(1)
    assert ca.data_url("sid-1", row) == "data:image/png;base64,iVBORw=="
    assert seen == [f"sessions/sid-1/attachments/{row['id']}/file"]
    assert ca.data_url("sid-1", {**row, "kind": "text"}) is None
    assert ca.data_url("sid-1", {**row, "id": "../x"}) is None
    assert ca.data_url("sid-1", {**row, "mime": "image/svg+xml"}) is None

    def broken(key):
        raise OSError("down")
    monkeypatch.setattr(ca, "get_bytes", broken)
    assert ca.data_url("sid-1", row) is None


@pytest.mark.parametrize("content", ["plain", [{"type": "text", "text": "a"}, {"type": "image_url",
                                                                              "image_url": {"url": "data:x"}}]])
def test_words_of_handles_both_shapes(content):
    assert "data:" not in T._words_of(content)
