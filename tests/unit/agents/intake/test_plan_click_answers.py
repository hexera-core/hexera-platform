# Responsibility: Verify NOTE-22: after "which one?" (NOTE-21), a click sent alone - no words, or only pointing words -
#       answers the waiting one-place ask: the ask is kept with the plan-step state, the click's note carries the
#       earlier words beside its own, the checklist and the run bind them to exactly the faces clicked, and the reply
#       says what was set where; words of the user's own drop it; any other turn keeps it one turn at most.
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from meshpipeline.agents.intake import plan_reply as PR
from meshpipeline.agents.intake import plan_talk as PT
from meshpipeline.application import part_matches as PM
from meshpipeline.cad import part_words as PW
from meshpipeline.contracts import user_notes as UN
from meshpipeline.engines import mesh_requirements as MR

SAID = "make it finer in this hole"
HOLES = [{"id": f"Feature_{k}", "feature": True, "kind": "hole", "count": 1, "in_part": "Part_1",
          "name": f"a hole, {6 + k} mm across", "diag_mm": float(6 + k)} for k in range(1, 12)]
TABLE = [{"id": "Part_1", "name": ""}, *HOLES]
HOLE_FACE = {"id": "Face_12", "label": "Face 12 · round Ø6 mm", "sig": {"area": 1.0, "c": [0, 0, 0]}}
FLAT_FACE = {"id": "Face_3", "label": "Face 3 · flat", "sig": {"area": 9.0, "c": [1, 0, 0]}}
ASKED = {"text": SAID, "source": "chat", "turn": 1}


def _reading():
    args = json.dumps({"phrases": [{"parts": [], "everything_else": False, "ask": "refine", "quote": SAID,
                                    "why": "a hole", "confidence": 0.4}], "words": []})
    unsure: list[dict] = []
    PW.check(args, TABLE, SAID, unsure)
    return {"status": "used", "entries": [], "unsure": unsure}


def _card(notes):
    own = UN.clean(notes)
    return {**PM.with_notes(PM.with_faces(PM.rows_of(_reading(), {"table": TABLE}, "s1"), own), own),
            "status": "done"}


def _click(face, text=""):
    return UN.make(text, source=UN.SOURCE_CHAT, turn=2, faces=[face], answers=SAID)


def test_the_rule_id_is_cited():
    assert PT.CLICK_RULE == UN.RULE_CLICK_ANSWERS == "NOTE-22"


@pytest.mark.parametrize("text", ["", "this one", "here", "that hole", "these", "this one please", "That's the one",
                                  "here it is", "this face"])
def test_pointing_words_only_point(text):
    assert PT.points_only(text)


@pytest.mark.parametrize("text", ["1 mm here", "finer here", "coarser on this one", "make this fillet 2 mm",
                                  "what about this?", "not this one", "use layers here"])
def test_words_of_their_own_are_not_pointing(text):
    assert not PT.points_only(text)


WAITING = {"words": SAID, "noun": "hole", "n": 11, "turns_left": 1}


def test_a_click_alone_answers_and_the_ask_is_used_up():
    assert PT.after_turn(WAITING, text="", faces=True) == (SAID, None)
    assert PT.after_turn(WAITING, text="this one", faces=True) == (SAID, None)


@pytest.mark.parametrize("text,faces", [("1 mm here", True), ("coarser on this face", True),
                                        ("make the fillet finer", False), ("2 mm everywhere", False)])
def test_words_of_their_own_win_and_the_ask_is_dropped(text, faces):
    assert PT.after_turn(WAITING, text=text, faces=faces) == ("", None)


def test_any_other_turn_keeps_it_one_turn_at_most():
    words, still = PT.after_turn(WAITING, text="what will the cell count be?", faces=False)
    assert words == "" and still is not None and still["turns_left"] == 0 and still["words"] == SAID
    assert PT.after_turn(still, text="hmm", faces=False) == ("", None)     # never longer, never applied unclicked
    assert PT.after_turn(None, text="", faces=True) == ("", None)


def test_the_which_one_reply_leaves_the_ask_waiting():
    card = _card([ASKED])
    assert PT.waiting_ask(ASKED, card, note_id="n1") == WAITING
    assert PT.waiting_ask(ASKED, None, note_id="n1") is None


def test_the_note_keeps_its_own_words_and_the_words_it_answers():
    note = _click(HOLE_FACE, "this one")
    assert note["text"] == "this one" and note["answers"] == SAID
    assert UN.said(note) == f"this one {SAID}" and UN.said(ASKED) == SAID
    kept = UN.merge([ASKED], [note])
    assert kept[1]["answers"] == SAID and kept[1]["text"] == "this one"
    # a later capture of the same turn (the message as sent) is not kept twice
    again = UN.make("this one", source=UN.SOURCE_CHAT, turn=2, faces=[HOLE_FACE])
    assert len(UN.merge(kept, [again])) == 2
    assert "this click answers the user's earlier words" in UN.render(kept)


@pytest.mark.parametrize("face,where", [(HOLE_FACE, "Face 12 (round Ø6 mm)"), (FLAT_FACE, "Face 3 (flat)")])
def test_the_click_sets_the_waiting_words_on_exactly_the_face_clicked(face, where):
    # a flat face clicked for "this hole" is still what is set: what the user did wins, and the reply names it
    click = _click(face)
    notes = [ASKED, click]
    card = _card(notes)
    assert card["unsure"] == []
    assert len(card["rows"]) == 1 and card["rows"][0]["faces"][0]["id"] == face["id"]
    assert card["rows"][0]["note"] == "n2"
    plain = PT.reply_for(click, card, note_id="n2")
    assert plain == f"I've set a finer mesh on {where}."
    f = PR.facts(click, card, note_id="n2")
    assert f["message"]["a_click_that_answers_their_earlier_words"] == SAID and PR.check(plain, f) == ""
    # the run: the same one rule binds the words to those faces
    assert [x["id"] for x in MR.picked_for(SAID, UN.clean(notes))] == [face["id"]]


def test_the_run_no_longer_tells_the_waiting_phrase_as_not_placed():
    req = {"local": [{"quote": SAID, "what": "finer", "faces": ["Face_12"], "sigs": [HOLE_FACE["sig"]],
                      "labels": [HOLE_FACE["label"]]}]}
    got = PW.with_picks(_reading(), req, surface=None)
    assert got["unsure"] == []


def test_a_message_with_no_click_is_still_asked_what_it_means():
    # the click alone with nothing waiting is answered as before
    bare = UN.make("", source=UN.SOURCE_CHAT, turn=2, faces=[HOLE_FACE])
    assert PT.reply_for(bare, _card([ASKED, bare]), note_id="n2").startswith("Got Face 12")


# the plan-step state: stored with the session's intake gate, so a reload keeps it

def _session(messages, notes, gate=None):
    return SimpleNamespace(id=uuid.UUID("bbbbcccc-2222-4222-b222-bbbbbbbbbbbb"), job_id=None, messages=messages,
                           user_notes=notes,
                           intake_gate=gate or {"approval": {"status": "awaiting", "expires_at": 4e9}})


@asynccontextmanager
async def _db():
    yield AsyncMock()


def test_the_which_one_reply_stores_the_ask_with_the_plan_step_state(monkeypatch):
    from meshpipeline.agents.intake import approval as ap
    monkeypatch.setattr(ap, "is_live", lambda a: bool(a))
    msgs = [{"role": "assistant", "content": "Here's the plan."}, {"role": "user", "content": SAID}]
    s = _session(msgs, [ASKED])
    repo = MagicMock()
    repo.get_for_owner = AsyncMock(return_value=s)
    repo.append_message = AsyncMock()
    repo.set_intake_gate = AsyncMock()

    async def _w(note, view, **_k):
        return PT.reply_for(note, view, note_id="n1"), PR.SOURCE_FALLBACK
    said = asyncio.run(PR.answer_owed(s, _card([ASKED]), owner_id="alice", session_repo=repo, db_factory=_db,
                                      write_fn=_w))
    assert "11 holes" in said
    gate = repo.set_intake_gate.await_args.args[2]
    assert gate[PT.WAITING] == WAITING and gate["approval"] == s.intake_gate["approval"]


def _settle(monkeypatch, text, faces, gate, reading):
    from meshpipeline.agents.intake import approval as ap
    from meshpipeline.agents.intake import message as M
    from meshpipeline.agents.intake import plan_go as PG

    async def _none(*_a, **_k):
        return None

    async def _read(*_a, **_k):
        return SimpleNamespace(reading=reading, by="code", why="test")
    monkeypatch.setattr(M, "_settle_unit_change", _none)
    monkeypatch.setattr(M, "_settle_route_change", _none)
    monkeypatch.setattr(PG, "read", _read)
    monkeypatch.setattr(ap, "is_live", lambda a: bool(a))
    monkeypatch.setattr(M.uc, "needs_confirmation", lambda locked: False)
    repo = MagicMock()
    repo.set_intake_gate = AsyncMock()
    repo.set_user_notes = AsyncMock()
    repo.append_message = AsyncMock()
    sid = uuid.UUID("bbbbcccc-2222-4222-b222-bbbbbbbbbbbb")
    msgs = [{"role": "user", "content": SAID}, {"role": "assistant", "content": "There are 11 holes..."},
            {"role": "user", "content": text, **({"faces": faces} if faces else {})}]
    inbound = M.InboundMessage(session_id=sid, owner_id="alice", content=text, faces=tuple(faces))
    out = asyncio.run(M._settle(inbound, AsyncMock(), gate=dict(gate), locked=SimpleNamespace(), messages=msgs,
                                revision="r1", session_repo=repo, logger=logging.getLogger("t")))
    stored = repo.set_intake_gate.await_args.args[2]
    notes = repo.set_user_notes.await_args.args[2] if repo.set_user_notes.await_args else []
    return out, stored, notes


GATE = {"approval": {"status": "awaiting", "expires_at": 4e9}, PT.WAITING: WAITING}


def test_a_click_sent_alone_is_kept_with_the_words_it_answers(monkeypatch):
    out, stored, notes = _settle(monkeypatch, "", [HOLE_FACE], GATE, PT.NOTE)
    assert out.note["answers"] == SAID and notes[0]["faces"][0]["id"] == "Face_12" and notes[0]["text"] == ""
    assert PT.WAITING not in stored


def test_a_click_with_words_of_its_own_drops_the_ask_unsaid(monkeypatch):
    out, stored, _n = _settle(monkeypatch, "1 mm here", [HOLE_FACE], GATE, PT.NOTE)
    assert "answers" not in out.note and PT.WAITING not in stored


def test_a_turn_with_no_click_keeps_it_once(monkeypatch):
    _o, stored, _n = _settle(monkeypatch, "wait", [], GATE, PT.HOLD)
    assert stored[PT.WAITING]["turns_left"] == 0
    _o, stored, _n = _settle(monkeypatch, "wait", [], stored, PT.HOLD)
    assert PT.WAITING not in stored
