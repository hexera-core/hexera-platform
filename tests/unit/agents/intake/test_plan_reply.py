# Responsibility: Verify the plan step's model-written reply under a guard (NOTE-19): the prompt carries the message
# and the facts the checklist produced, the guard passes good replies and refuses each bad kind (the plain sentence is
# said instead), a slow or failing model gives the plain sentence, rows are this message's by their note id (not by
# their words), "wait" starts nothing, and a plan change with a size switches the plan and keeps the size.
from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from meshpipeline.agents.intake import plan_reply as PR
from meshpipeline.agents.intake import plan_talk as PT
from meshpipeline.application import part_matches as PM

FACE32 = {"id": "Face_32", "label": "Face 32 · round Ø6 mm"}
NOTE = {"text": "can you do a 1 mm mesh sizing in here?", "faces": [FACE32], "source": "chat", "turn": 3}
ROW = {"phrase": 0, "note": "n2", "quote": "can you do a 1 mm mesh sizing in here?", "words": "can you do a 1 mm…",
       "size": "1 mm", "faces": [FACE32], "picked": True, "parts": []}
GUESSED_NOTE = {"text": "in that crevice there I want a very refined mesh", "source": "chat", "turn": 4}
GUESSED = {"note": "n3", "quote": "in that crevice there I want a very refined mesh", "size": "finer",
           "parts": ["Feature_3"], "parts_text": "the fillet (radius 8 mm)"}


def _done(rows=(), unsure=()):
    return {"status": "done", "rows": list(rows), "unsure": list(unsure)}


def test_the_rule_id_is_cited():
    assert PR.RULE == "NOTE-19"


# the prompt

def test_the_prompt_carries_the_message_and_the_facts_and_is_composed_from_its_registry():
    msgs = PR.messages_for(NOTE, _done([ROW]), note_id="n2", last_reply="Here's the plan.")
    system, user = msgs[0]["content"], msgs[1]["content"]
    names = [b[0] for b in PR.PLAN_REPLY_PROMPT_BLOCKS]
    assert names[:3] == ["voice", "facts_only", "guess_and_picture"] and names[-2:] == ["examples", "language"]
    assert "count" in names                                                     # NOTE-24
    assert system == PR.compose_system() and all(purpose and callable(b) for _, purpose, b in PR.PLAN_REPLY_PROMPT_BLOCKS)
    assert "first person" in system and "Never use a number that is not in the facts" in system
    body = json.loads(user.split("\n", 1)[1])
    f = body["facts"]
    assert f["message"]["words"] == NOTE["text"] and f["message"]["faces_picked"] == ["Face 32 · round Ø6 mm"]
    assert f["applied"] == [{"what": "1 mm elements", "where": "Face 32 (round Ø6 mm)",
                             "place": "they clicked it on the model"}]
    assert f["plain_version_that_is_exactly_right"] == "I've set 1 mm elements on Face 32 (round Ø6 mm)."
    assert body["your_last_reply"] == "Here's the plan."


def test_the_facts_say_a_guess_a_place_not_found_and_a_number_only_in_a_picture():
    note = {**GUESSED_NOTE, "attachments": [{"id": "a1", "kind": "image", "mime": "image/png"}]}
    unsure = {"note": "n3", "quote": "1 mm on the lip", "words": "1 mm on the lip", "size": "1 mm"}
    f = PR.facts(note, _done([GUESSED], [unsure]), note_id="n3")
    assert "guess" in f["applied"][0]["place"] and "click it on the model" in f["applied"][0]["place"]
    assert f["could_not_place"] == ["1 mm on the lip"]
    assert f["message"]["pictures"] == 1 and "do not read pictures" in f["picture"] and "number" not in f["picture"]
    assert PR.facts(GUESSED_NOTE, _done([GUESSED]), note_id="n3")["picture"] == ""      # no picture, no line


# the guard

GOOD = [
    (NOTE, _done([ROW]), "n2", "All right, 1 mm on Face 32 (round Ø6 mm)."),
    (NOTE, _done([ROW]), "n2", "Sounds good. Face 32 gets 1 mm elements."),
    (GUESSED_NOTE, _done([GUESSED]), "n3", "Got it, finer mesh at the fillet. That place is my guess, so click the "
                                           "model if you meant somewhere else."),
    ({"text": "make it look nice", "source": "chat", "turn": 5}, _done(), "",
     "I couldn't turn that into a mesh setting. Give me a size like 2 mm, or click a face on the model."),
    ({"text": "D'accord, 1 mm ici", "faces": [FACE32], "source": "chat", "turn": 6},
     _done([{**ROW, "quote": "1 mm ici"}]), "n2", "D'accord, 1 mm sur Face 32 (round Ø6 mm)."),
]


@pytest.mark.parametrize("note,view,nid,reply", GOOD)
def test_the_guard_passes_a_good_reply(note, view, nid, reply):
    assert PR.check(reply, PR.facts(note, view, note_id=nid)) == "", reply


BAD = [
    ("a number not in the facts", NOTE, _done([ROW]), "n2", "All right, 2 mm on Face 32 (round Ø6 mm)."),
    ("no place named", NOTE, _done([ROW]), "n2", "All right, 1 mm it is."),
    ("claims something was applied", {"text": "make it look nice", "source": "chat", "turn": 5}, _done(), "",
     "Done, I've set that for you."),
    ("too long", NOTE, _done([ROW]), "n2", "All right, 1 mm on Face 32 (round Ø6 mm). " + "And so on. " * 30),
    ("a rule id", NOTE, _done([ROW]), "n2", "Per NOTE-18, 1 mm on Face 32 (round Ø6 mm)."),
    ("an exclamation", NOTE, _done([ROW]), "n2", "Certainly! 1 mm on Face 32 (round Ø6 mm)."),
]


@pytest.mark.parametrize("kind,note,view,nid,reply", BAD, ids=[b[0] for b in BAD])
def test_the_guard_refuses_each_bad_kind_and_the_plain_sentence_is_said(kind, note, view, nid, reply):
    assert PR.check(reply, PR.facts(note, view, note_id=nid)) != "", kind
    got = asyncio.run(_write_with(reply, note, view, nid))
    assert got == (PT.reply_for(note, view, note_id=nid), PR.SOURCE_FALLBACK), kind


def test_the_same_opener_as_the_last_reply_is_no_longer_refused_variety_is_asked_of_the_prompt():
    f = PR.facts(NOTE, _done([ROW]), note_id="n2")
    assert PR.check("All right, 1 mm on Face 32.", f, last_reply="All right, finer mesh at the fillet.") == ""
    assert "a different one each time" in PR.compose_system()


def _stub(monkeypatch, answer=None, *, raises=None, sleep=0.0):
    seen = {}

    async def _model(messages, *, job_id, user_id):
        seen["messages"] = messages
        if sleep:
            await asyncio.sleep(sleep)
        if raises:
            raise raises
        return answer
    monkeypatch.setattr(PR, "_ask", _model)
    return seen


def _write_with(reply, note, view, nid):
    async def run():
        orig = PR._ask

        async def _model(messages, *, job_id, user_id):
            return reply
        PR._ask = _model
        try:
            return await PR.write(note, view, note_id=nid)
        finally:
            PR._ask = orig
    return run()


def test_a_good_model_reply_is_said_and_its_source_kept(monkeypatch):
    seen = _stub(monkeypatch, "All right, 1 mm on Face 32 (round Ø6 mm).")
    got = asyncio.run(PR.write(NOTE, _done([ROW]), note_id="n2"))
    assert got == ("All right, 1 mm on Face 32 (round Ø6 mm).", PR.SOURCE_MODEL)
    assert seen["messages"][0]["content"] == PR.compose_system()


def test_a_failing_model_gives_the_plain_sentence(monkeypatch):
    _stub(monkeypatch, raises=RuntimeError("provider down"))
    assert asyncio.run(PR.write(NOTE, _done([ROW]), note_id="n2")) == (
        "I've set 1 mm elements on Face 32 (round Ø6 mm).", PR.SOURCE_FALLBACK)


def test_a_slow_model_gives_the_plain_sentence(monkeypatch):
    _stub(monkeypatch, "All right, 1 mm on Face 32 (round Ø6 mm).", sleep=0.5)
    assert asyncio.run(PR.write(NOTE, _done([ROW]), note_id="n2", timeout_s=0.05)) == (
        "I've set 1 mm elements on Face 32 (round Ø6 mm).", PR.SOURCE_FALLBACK)
    assert PR.TIMEOUT_S <= 6.0


def test_a_reading_still_going_or_failed_is_said_plainly_with_no_model(monkeypatch):
    _stub(monkeypatch, raises=AssertionError("the model must not be asked"))
    assert asyncio.run(PR.write(NOTE, {"status": "reading"})) == (PT.STILL_READING, PR.SOURCE_FIXED)
    assert asyncio.run(PR.write(NOTE, {**_done(), "failed": True})) == (PT.READ_FAILED, PR.SOURCE_FIXED)


# rows are this message's by the note's own id

def test_a_row_is_matched_by_its_note_id_even_when_its_quote_is_reworded():
    note = {"text": "can you do a 1 mm mesh sizing in here?", "faces": [FACE32]}
    reworded = {**ROW, "quote": "1 mm mesh sizing here"}       # not word for word in the message
    other = {**ROW, "note": "n1", "quote": "can you do a 1 mm mesh sizing in here?"}   # another note's row
    assert PT.reply_for(note, _done([reworded, other]), note_id="n2") == "I've set 1 mm elements on Face 32 (round Ø6 mm)."
    assert PT.mine(note, _done([other]), note_id="n2") == ([], [])


def test_the_reading_stamps_each_row_with_the_note_it_was_read_from():
    notes = [{"text": "2 mm on the frame rail", "source": "console"},
             {"text": "Please: 1 mm  on the bore, thanks", "source": "chat", "turn": 3}]
    got = PM.with_notes({"status": "done", "rows": [{"quote": "1 mm on the bore"}, {"quote": "2 mm on the frame rail"}],
                         "unsure": [{"quote": "something else"}]}, notes)
    assert [r.get("note") for r in got["rows"]] == ["n2", "n1"] and "note" not in got["unsure"][0]
    assert PM.note_id(notes, {"source": "chat", "turn": 3, "text": "x"}) == "n2"


# the reply owed, written once, through the poll as through the send

def _session(messages, notes, approval_live=True):
    from meshpipeline.agents.intake import approval as ap
    live = {"status": ap.AWAITING, "expires_at": 4e9, "id": "a1"} if approval_live else None
    return SimpleNamespace(id=uuid.UUID("bbbbcccc-2222-4222-b222-bbbbbbbbbbbb"), job_id=None, messages=messages,
                           user_notes=notes, intake_gate={"approval": live})


def test_the_reply_owed_is_written_once_and_kept(monkeypatch):
    from meshpipeline.agents.intake import approval as ap
    monkeypatch.setattr(ap, "is_live", lambda a: bool(a))
    msgs = [{"role": "assistant", "content": "Here's the plan."}, {"role": "user", "content": "1 mm here"}]
    notes = [{"text": "1 mm here", "source": "chat", "turn": 1, "faces": [FACE32]}]
    s = _session(msgs, notes)
    repo = MagicMock()
    repo.get_for_owner = AsyncMock(return_value=s)
    repo.append_message = AsyncMock(side_effect=lambda db, sid, role, text: msgs.append({"role": role, "content": text}))

    @asynccontextmanager
    async def _db():
        yield AsyncMock()

    async def _w(note, view, **kw):
        assert kw["note_id"] == "n1" and kw["last_reply"] == "Here's the plan."
        return "Sounds good, 1 mm on Face 32 (round Ø6 mm).", PR.SOURCE_MODEL
    view = _done([{**ROW, "note": "n1"}])
    first = asyncio.run(PR.answer_owed(s, view, owner_id="alice", session_repo=repo, db_factory=_db, write_fn=_w))
    again = asyncio.run(PR.answer_owed(s, view, owner_id="alice", session_repo=repo, db_factory=_db, write_fn=_w))
    assert first == "Sounds good, 1 mm on Face 32 (round Ø6 mm)." and again == ""
    assert repo.append_message.await_count == 1


# "wait", and a plan change with a size (the chat route, the model stubbed)

def _kept(monkeypatch):
    from meshpipeline.agents.intake import message as M
    kept = []

    async def _keep(inbound, db, **_kw):
        kept.append(inbound.content)
        return {"text": inbound.content}
    monkeypatch.setattr(M, "_keep_requirement", _keep)
    return kept


def test_wait_starts_nothing_keeps_nothing_and_says_so(monkeypatch):
    from test_intake_approval_lifecycle import _approval, _route
    kept = _kept(monkeypatch)
    for said in ("wait", "don't proceed yet", "not yet, go later", "hold on"):
        rec, sel = _approval(msg_count=1)
        resp, calls, repo = _route(said, rec, sel)
        assert resp.reply == PT.HOLD_LINE, said
        assert calls == {"intake": 0, "dispatch": 0}, said
    assert kept == []
    assert PT.reading("wait") == PT.HOLD and PT.reading("don't proceed yet") == PT.HOLD


@pytest.mark.parametrize("landed", [False, True])
def test_the_send_never_waits_long_for_the_reading_the_reply_then_comes_with_the_poll(monkeypatch, landed):
    from test_intake_approval_lifecycle import _approval, _route

    from meshpipeline.api.v1 import chat as chat_mod
    _kept(monkeypatch)
    assert chat_mod.REQUIREMENT_READ_WAIT_S + PR.TIMEOUT_S <= 20      # no send is held much past 20 s
    view = _done([ROW]) if landed else {"status": "reading"}
    monkeypatch.setattr(PM, "finalize", lambda s, owner_id="": view)
    monkeypatch.setattr(PM, "view", lambda s, owner_id="": view)
    monkeypatch.setattr(chat_mod, "REQUIREMENT_READ_WAIT_S", 0.05)
    monkeypatch.setattr(chat_mod, "REQUIREMENT_POLL_S", 0.01)

    async def _brief(*_a, **_k):
        return None
    monkeypatch.setattr(chat_mod, "_brief", _brief)
    said = []

    async def _owed(session, v, **_k):
        said.append(v)
        return "Sounds good, 1 mm on Face 32 (round Ø6 mm)."
    monkeypatch.setattr(PR, "answer_owed", _owed)
    rec, sel = _approval(msg_count=1)
    resp, calls, _ = _route("1 mm on the bore", rec, sel)
    if landed:
        assert resp.reply == "Sounds good, 1 mm on Face 32 (round Ø6 mm)." and not resp.reply_later and said == [view]
    else:
        assert resp.reply == "" and resp.reply_later is True and said == []   # the poll brings it


def test_a_plan_change_with_a_size_switches_the_plan_and_keeps_the_size(monkeypatch):
    from test_intake_approval_lifecycle import _approval, _route

    from meshpipeline.agents.intake import approval as ap
    kept = _kept(monkeypatch)
    rec, sel = _approval(msg_count=1)
    resp, calls, repo = _route("use cfMesh at 2 mm", rec, sel)
    assert calls["intake"] == 1 and calls["dispatch"] == 0          # the intake switches the plan
    assert repo.set_intake_gate.call_args_list[0][0][2]["approval"]["status"] == ap.INVALIDATED
    assert kept == ["use cfMesh at 2 mm"]                           # and the size is kept as a requirement
    assert PT.reading("use cfMesh at 2 mm", engine="gmsh") == PT.BOTH
    assert PT.reading("use cfMesh at 2 mm", engine="cfmesh") == PT.NOTE
