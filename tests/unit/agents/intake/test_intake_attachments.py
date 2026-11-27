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


def test_without_vision_the_model_is_told_it_cannot_see_and_that_the_user_is_told():
    state = [{"role": "user", "content": "", "attachments": [_pic(1, "bore.png")]}]
    calls = []
    out = A._with_pictures(_llm(state), state, vision=False, load=lambda r: calls.append(r) or "x")
    assert calls == [] and isinstance(out[1]["content"], str)
    assert "bore.png" in out[1]["content"] and "cannot see pictures" in out[1]["content"]
    assert A.NO_VISION_LINE not in out[1]["content"]          # said by the application, not asked of the model
    assert A.NO_VISION_LINE == "I could not look at the picture here; it is kept with your notes."
    assert A._shows_a_picture(out) is False


# the no-vision line, in code
_PIC_TURN = {"role": "user", "content": "this bore at 2 mm", "attachments": [_pic(1, "bore.png")]}


def test_the_line_is_added_once_when_the_turn_carried_a_picture_the_model_did_not_see():
    got = A._with_no_vision_line("Got it: 2 mm on the bore.", [_PIC_TURN], shown=False)
    assert got == "Got it: 2 mm on the bore.\n\n" + A.NO_VISION_LINE
    assert A._with_no_vision_line(got, [_PIC_TURN], shown=False) == got            # never twice
    assert A._with_no_vision_line("", [_PIC_TURN], shown=False) == A.NO_VISION_LINE


def test_no_line_when_the_model_saw_it_or_the_turn_had_no_picture_or_on_a_later_turn():
    assert A._with_no_vision_line("ok", [_PIC_TURN], shown=True) == "ok"
    assert A._with_no_vision_line("ok", [{"role": "user", "content": "2 mm"}], shown=False) == "ok"
    later = [_PIC_TURN, {"role": "assistant", "content": "ok\n\n" + A.NO_VISION_LINE},
             {"role": "user", "content": "and 3 mm elsewhere"}]
    assert A._with_no_vision_line("ok", later, shown=False) == "ok"
    answered = [_PIC_TURN, {"role": "assistant", "content": "ok"}]                # not this turn's message
    assert A._with_no_vision_line("more", answered, shown=False) == "more"
    nudged = [_PIC_TURN, {"role": "user", "content": "nudge", "_synthetic": True}]
    assert A._with_no_vision_line("ok", nudged, shown=False).endswith(A.NO_VISION_LINE)


def test_a_picture_shown_is_seen_as_shown():
    state = [_PIC_TURN]
    assert A._shows_a_picture(A._with_pictures(_llm(state), state, vision=True, load=_load)) is True
    assert A._shows_a_picture(A._with_pictures(_llm(state), state, vision=True, load=lambda r: None)) is False


def _turn(messages, reply):
    import asyncio
    from unittest.mock import patch

    import meshpipeline.contracts.model_inference as llm
    from meshpipeline.contracts.model_inference import ModelRoundResult

    async def _call(**_kw):
        return ModelRoundResult(tool_calls=(), assistant_text=reply, finish_reason="stop")
    with patch.object(llm, "call_intake_model", _call):
        return asyncio.run(A.node_intake({"job_id": "j", "session_id": "s", "user_id": "u", "messages": messages}))


def test_the_intake_turn_ends_with_the_line_once_and_the_next_turn_does_not_repeat_it():
    first = _turn([{**_PIC_TURN, "content": "external aero on a wing, snappy"}], "Noted.")
    reply = first["messages"][-1]["content"]
    assert reply.count(A.NO_VISION_LINE) == 1 and reply.rstrip().endswith(A.NO_VISION_LINE)
    second = _turn([*first["messages"], {"role": "user", "content": "30 m/s"}], "Noted.")
    assert A.NO_VISION_LINE not in second["messages"][-1]["content"]


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
