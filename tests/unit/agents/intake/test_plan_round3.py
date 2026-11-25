# Responsibility: Verify round three of the plan step as a conversation (NOTE-18, NOTE-19), the model stubbed: "go" read
# robustly (code decides the safe cases, the intake's model the rest, the phrase reading when it does not answer);
# replies that answer the person under a guard that lets natural replies through; and the seconds a requirement costs.
from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

import pytest

from meshpipeline.agents.intake import consent as C
from meshpipeline.agents.intake import plan_go as PG
from meshpipeline.agents.intake import plan_reply as PR
from meshpipeline.agents.intake import plan_talk as PT
from meshpipeline.application import part_matches as PM

PLAN_LINE = "Here's the plan. Tell me any mesh requirements here, then say go or press Proceed."

#: everyday ways of saying "I'm done here, mesh it" (item 1: at least 30 beyond the round-one table)
MORE_GO = [
    "ok I'm good to mesh", "ok im good", "yep run it", "alright that's everything", "that's all from me, mesh it",
    "fine, go ahead", "lets go", "do it", "send it", "looks good, start", "nothing else, proceed", "no more changes",
    "we're done here, mesh", "all right, I'm good to mesh", "let's proceed to mesh generation", "ok go",
    "sounds good, mesh it", "great, go", "im done", "good to go", "ready", "yes please start", "start it",
    "perfect, run it", "go for it", "alright lets mesh", "ok thats it", "k, go", "proceeed", "mesh it pls",
    "that's everything, thanks", "we're good", "carry on", "ok, start meshing", "happy with that, go",
    "OK IM GOOD TO MESH", "yeah thats all lets go",
]
#: never a go, whatever the model says (item 1: at least 15)
NOT_GO = [
    "go finer on the bore", "proceed with 2 mm on the holes", "is it good to mesh like this?",
    "what happens if I proceed?", "not yet", "hold on", "go ahead but make the fillets finer", "don't start yet",
    "wait, one more thing", "run it at 1 mm", "can we refine the bore first?", "no, not yet", "stop",
    "yes but coarser on the plate", "let's go with 3 layers", "start with 0.5 mm on the fillets", "go later",
    "proceed?", "ready to mesh?", "use cfMesh instead",
]


def _answers(label):
    calls = []

    async def ask(messages, *, job_id, user_id):
        calls.append(messages)
        return label
    return ask, calls


async def _fails(*_a, **_k):
    raise RuntimeError("provider down")


def _read(text, *, ask_fn, last_line=PLAN_LINE, carries=False, asked=False, timeout_s=PG.TIMEOUT_S):
    return asyncio.run(PG.read(text, carries=carries, asked=asked, consent=C.reading(text), last_line=last_line,
                               ask_fn=ask_fn, timeout_s=timeout_s))


# item 1: go read robustly

def test_the_lists_are_long_enough():
    assert len(MORE_GO) >= 30 and len(NOT_GO) >= 15


@pytest.mark.parametrize("text", MORE_GO)
def test_a_go_in_everyday_words_starts_the_run_when_the_model_says_go(text):
    ask, calls = _answers("go")
    got = _read(text, ask_fn=ask)
    assert got.reading == PT.GO, text
    assert got.by in (PG.BY_MODEL, PG.BY_CODE)


@pytest.mark.parametrize("text", MORE_GO)
def test_with_no_model_a_go_is_never_answered_as_an_unread_requirement(text):
    got = _read(text, ask_fn=_fails)
    assert got.reading in (PT.GO, PT.ASK), text          # never NOTE ("I couldn't read that as a mesh requirement")


def test_the_live_failure_is_fixed_in_the_phrase_reading_too():
    # the live run: "ok I'm good to mesh" was read as a requirement four times
    assert PT.reading("ok I'm good to mesh", consent=C.reading("ok I'm good to mesh")) == PT.GO
    assert _read("ok I'm good to mesh", ask_fn=_fails).reading == PT.GO


@pytest.mark.parametrize("text", NOT_GO)
def test_never_a_go_even_when_the_model_says_go(text):
    ask, _ = _answers("go")
    assert _read(text, ask_fn=ask).reading != PT.GO, text
    assert _read(text, ask_fn=_fails).reading != PT.GO, text


def test_ok_after_a_question_that_was_not_about_starting_is_not_a_go():
    ask, _ = _answers("go")
    q = "Do you want the fillets finer as well?"
    assert _read("ok", ask_fn=ask, last_line=q).reading == PT.INTAKE
    assert _read("ok", ask_fn=_fails, last_line=q).reading == PT.INTAKE
    assert _read("ok", ask_fn=ask, last_line=PT.ASK_LINE).reading == PT.GO      # "Start the mesh now?" - "ok"


def test_code_decides_the_safe_cases_and_the_model_is_not_asked():
    ask, calls = _answers("go")
    assert _read("go finer on the bore", ask_fn=ask).reading == PT.NOTE
    assert _read("proceed with 2 mm on the holes", ask_fn=ask).reading == PT.NOTE
    assert _read("all good, go", ask_fn=ask, carries=True).reading == PT.NOTE     # a face or picture came with it
    assert _read("Proceed", ask_fn=ask).reading == PT.GO                         # the button's text
    assert _read("use cfMesh instead", ask_fn=ask).reading == PT.INTAKE          # a plan word
    assert calls == []


@pytest.mark.parametrize("label,want", [("go", PT.GO), ("wait", PT.HOLD), ("question", PT.INTAKE),
                                        ("requirement", PT.NOTE), ("other", PT.ASK), ("Go.", PT.GO),
                                        ("go - they want to start", None)])
def test_each_model_answer_and_a_loose_answer_falls_back(label, want):
    ask, calls = _answers(label)
    got = _read("all right then", ask_fn=ask)
    assert len(calls) == 1
    if want is None:      # not exactly one word of the set: the phrase reading decides
        assert got.by == PG.BY_FALLBACK and got.reading in (PT.GO, PT.ASK)
    else:
        assert got.reading == want and got.by == PG.BY_MODEL


def test_the_model_question_is_small_and_strict():
    ask, calls = _answers("go")
    _read("yep run it", ask_fn=ask, last_line="Here's the plan.")
    system, user = calls[0][0]["content"], calls[0][1]["content"]
    assert system == PG.compose_system() and [b[0] for b in PG.PLAN_GO_PROMPT_BLOCKS] == ["task", "labels", "rules"]
    assert all(f"\n{w} - " in "\n" + system for w in PG.LABELS)
    assert '"assistant_last_line": "Here\'s the plan."' in user and '"user_message": "yep run it"' in user


def test_a_slow_model_gives_the_phrase_reading_and_asks_when_unsure():
    async def slow(*_a, **_k):
        await asyncio.sleep(0.5)
        return "go"
    t0 = time.monotonic()
    got = _read("alright, we're sorted", ask_fn=slow, timeout_s=0.05)
    assert time.monotonic() - t0 < 0.4 and got.by == PG.BY_FALLBACK
    assert got.reading == PT.ASK                                 # unsure: "Start the mesh now?" once
    assert _read("alright, we're sorted", ask_fn=_fails, asked=True).reading == PT.INTAKE   # asked already
    assert PG.TIMEOUT_S == 3.0


def _route_with(monkeypatch, said, label):
    from test_intake_approval_lifecycle import _approval, _route
    monkeypatch.setattr(PG, "_ask", (_answers(label)[0] if label else _fails))
    rec, sel = _approval(msg_count=1)
    return _route(said, rec, sel)


@pytest.mark.parametrize("label", ["go", None])
def test_the_live_sentence_starts_the_run_through_the_chat_route(monkeypatch, label):
    resp, calls, _ = _route_with(monkeypatch, "ok I'm good to mesh", label)
    assert calls == {"intake": 0, "dispatch": 1}


def test_a_go_like_sentence_the_model_is_unsure_of_is_asked_never_told_it_is_unreadable(monkeypatch):
    resp, calls, _ = _route_with(monkeypatch, "alright, we're sorted", "other")
    assert resp.reply == PT.ASK_LINE and calls == {"intake": 0, "dispatch": 0}
    assert "couldn't read" not in resp.reply


def test_wait_from_the_model_is_held(monkeypatch):
    resp, calls, _ = _route_with(monkeypatch, "give me a sec", "wait")
    assert resp.reply == PT.HOLD_LINE and calls == {"intake": 0, "dispatch": 0}


# item 2: replies that answer the person

FACE23 = {"id": "Face_23", "label": "Face 23 · round Ø19.8 mm"}
THREE = [{"id": f"Face_{k}", "label": f"Face {k} · flat 10 × 10 mm"} for k in (4, 5, 6)]


def _done(rows=(), unsure=()):
    return {"status": "done", "rows": list(rows), "unsure": list(unsure)}


ONE = ({"text": "can you do a 0.5 mm mesh sizing in here?", "faces": [FACE23]},
       _done([{"quote": "can you do a 0.5 mm mesh sizing in here?", "size": "0.5 mm", "faces": [FACE23],
               "picked": True, "parts": []}]))
SEVERAL = ({"text": "1 mm on these too", "faces": THREE},
           _done([{"quote": "1 mm on these too", "size": "1 mm", "faces": THREE, "picked": True, "parts": []}]))
PIC = [{"id": "a1", "kind": "image", "mime": "image/png"}]
GUESS = ({"text": "in that big hole I want a refined mesh, see the picture", "attachments": PIC},
         _done([{"quote": "in that big hole I want a refined mesh, see the picture", "size": "finer",
                 "parts": ["Feature_2"], "parts_text": "the hole (22 mm across)"}]))
WHOLE = ({"text": "keep skewness under 0.8"},
         _done([{"quote": "keep skewness under 0.8", "words": "keep skewness under 0.8", "size": "",
                 "parts": [], "parts_text": "the whole model"}]))
NOTHING = ({"text": "make it look nice"}, _done())

NATURAL = [
    (ONE, "Sure, I've set 0.5 mm elements on Face 23."),
    (ONE, "Yep, done. That face gets 0.5 mm elements."),
    (SEVERAL, "Got it, those 3 faces get 1 mm elements as well."),
    (SEVERAL, "Okay, 1 mm on the three faces you picked too."),
    (GUESS, "Okay, I'll go finer in that hole. I took it as the 22 mm one, so click it on the model if you meant "
            "another spot. I keep the picture with your note, but I don't read pictures myself."),
    (WHOLE, "Will do, I'll hold skewness under 0.8 across the whole mesh."),
    (NOTHING, "I couldn't find a mesh setting in that. Give me a size, or click a face and tell me what you want."),
]


@pytest.mark.parametrize("case,reply", NATURAL)
def test_the_guard_lets_natural_first_person_replies_through(case, reply):
    note, view = case
    assert PR.check(reply, PR.facts(note, view)) == "", reply


REFUSED = [
    ("no place named", ONE, "Sure, done."),
    ("a number not in the facts", ONE, "Sure, I've set 0.8 mm elements on Face 23."),
    ("a guessed place not said as a guess", GUESS, "Okay, I'll go finer in the 22 mm hole."),
    ("claims something was applied when nothing was", NOTHING, "Got it, I've set that for you."),
    ("an exclamation, filler or emoji", ONE, "Sure! 0.5 mm on Face 23."),
    ("too long", ONE, "Sure, I've set 0.5 mm elements on Face 23. " + "More words here. " * 20),
]


@pytest.mark.parametrize("why,case,reply", REFUSED, ids=[r[0] for r in REFUSED])
def test_the_guard_still_refuses_what_is_not_safe_and_says_which_check(why, case, reply, caplog):
    note, view = case
    assert PR.check(reply, PR.facts(note, view)).startswith(why)

    async def model(*_a, **_k):
        return reply
    with caplog.at_level(logging.INFO, logger=PR.__name__):
        got = asyncio.run(_with_model(model, note, view))
    assert got["source"] == PR.SOURCE_FALLBACK and got["why"].startswith(why) and got["raw"] == reply.strip()
    assert f"refused by the check '{why}" in caplog.text


async def _with_model(model, note, view):
    orig = PR._ask
    PR._ask = model
    try:
        return await PR.written(note, view)
    finally:
        PR._ask = orig


def test_the_prompt_answers_the_person_with_examples_and_asks_for_variety():
    system = PR.compose_system()
    assert "first person" in system and "Sure" in system and "Got it" in system and "Vary your wording" in system
    assert system.count("They: '") >= 4                                   # few-shot examples in that voice
    assert "Sure, I've set 0.5 mm elements on Face 23." in system
    assert "the system's guess" not in system and "!" not in system.replace("No exclamation", "")


def test_the_fallback_sentences_speak_in_the_first_person_and_the_picture_line_is_true():
    assert PT.reply_for(*SEVERAL) == "I've set 1 mm elements on the 3 faces you picked."
    got = PT.reply_for(*GUESS)
    assert got.startswith("I've made the mesh finer at the hole (22 mm across).")
    assert "my guess" in got and "system" not in got
    assert got.endswith(PT.PICTURE_NOTE) and got.count("picture") == 2 and "number" not in got
    assert "picture" not in PT.reply_for(*ONE)                            # only when a picture came


def test_the_facts_carry_a_picture_line_only_with_a_picture_and_say_a_question_was_asked():
    f = PR.facts(*ONE)
    assert f["picture"] == "" and f["message"]["is_a_question"] is True
    assert "do not read pictures" in PR.facts(*GUESS)["picture"]


# item 3: speed

@pytest.mark.parametrize("text,faces,want", [
    ("1 mm on these too", THREE, "1 mm"),
    ("can you do a 0.5 mm mesh sizing in here?", [FACE23], "0.5 mm"),
    ("0,8mm here", [FACE23], "0.8 mm"),
    ("1 mm on these too", [], None),                       # no faces: the reader must find the place
    ("finer here", [FACE23], None),                        # no size
    ("1 mm here and 3 layers", [FACE23], None),            # more than a size
    ("1-2 mm here", [FACE23], None),                       # a range
    ("2 cm here", [FACE23], None),                         # another unit
    ("keep it under 1 mm", [FACE23], None),                # a limit
])
def test_the_short_way_takes_only_a_plain_size_on_picked_faces(text, faces, want):
    rows = PT.plain_face_rows({"text": text, "faces": faces})
    assert (rows[0]["size"] if rows else None) == want, text
    if rows:
        assert rows[0]["faces"] == [{"id": f["id"], "label": f["label"]} for f in faces]
        assert PT.reply_for({"text": text, "faces": faces}, _done(rows)).startswith(f"I've set {want} elements on")


def test_a_face_and_a_size_is_answered_without_waiting_for_the_reading(monkeypatch):
    """BEFORE: the send waited for the reading (up to REQUIREMENT_READ_WAIT_S, here 1 s) and then the reply call.
    AFTER: faces picked + one plain size - the reply is written at once from the known row."""
    from test_intake_approval_lifecycle import _approval, _route

    from meshpipeline.agents.intake import message as M
    from meshpipeline.api.v1 import chat as chat_mod

    async def _keep(inbound, db, **_kw):
        return {"text": inbound.content}
    monkeypatch.setattr(M, "_keep_requirement", _keep)
    monkeypatch.setattr(PM, "finalize", lambda s, owner_id="": {"status": "reading"})
    monkeypatch.setattr(PM, "view", lambda s, owner_id="": {"status": "reading"})
    monkeypatch.setattr(chat_mod, "REQUIREMENT_READ_WAIT_S", 1.0)
    note = {"text": "1 mm on these too", "faces": THREE, "source": "chat", "turn": 2}
    monkeypatch.setattr(PR, "owed_note", lambda s: dict(note))

    async def _brief(*_a, **_k):
        return None
    monkeypatch.setattr(chat_mod, "_brief", _brief)
    seen = []

    async def _owed(session, v, **_k):
        seen.append(v)
        await asyncio.sleep(0.05)                          # the reply call, stubbed
        return PT.reply_for(note, v)
    monkeypatch.setattr(PR, "answer_owed", _owed)
    rec, sel = _approval(msg_count=1)
    t0 = time.monotonic()
    resp, calls, _ = _route("1 mm on these too", rec, sel)
    took = time.monotonic() - t0
    assert resp.reply == "I've set 1 mm elements on the 3 faces you picked." and not resp.reply_later
    assert took < 0.6, took                                # before: >= 1.0 s here (the whole wait), and no reply
    assert seen and seen[0]["rows"][0]["size"] == "1 mm"


def test_the_flow_routes_two_readings_run_side_by_side(monkeypatch):
    """BEFORE: the parts reading and the flow reader's reading ran one after the other (0.3 + 0.3 s stubbed).
    AFTER: side by side (about 0.3 s)."""
    from meshpipeline.application import geometry_check as GC
    from meshpipeline.cad import part_words as PW
    from meshpipeline.contracts import object_storage as OS
    store: dict = {}
    monkeypatch.setattr(GC, "read_check", lambda sid: {})
    monkeypatch.setattr(PM, "parts_seen", lambda check: {"route": PM.ROUTE_CFD, "table": [{"id": "Part_1"}],
                                                         "pictures": []})
    monkeypatch.setattr(PM, "_route_ask", lambda route: ("c", "p", True))
    monkeypatch.setattr(OS, "get_object_store", lambda: SimpleNamespace(download_file=lambda **_k: None))
    monkeypatch.setattr(PM, "_read", lambda sid, name: store.get(name))
    monkeypatch.setattr(PM, "_write", lambda sid, name, payload: store.__setitem__(name, payload))

    async def _ask(*_a, **_k):
        await asyncio.sleep(0.3)
        return {"status": "used", "entries": []}
    monkeypatch.setattr(PW, "ask", _ask)

    def _notes(state, sid):
        time.sleep(0.3)
        return {"status": "used"}
    monkeypatch.setattr(PM, "read_notes", _notes)
    t0 = time.monotonic()
    got = PM.run_reading(session_id="s1", key="k1", user_notes=[{"text": "2 mm on the inlet"}])
    took = time.monotonic() - t0
    assert got["status"] == "done" and store[PM.MATCHES]["notes"] == {"status": "used"}
    assert took < 0.5, took                                # before: >= 0.6 s


def test_the_send_looks_for_the_landed_reading_four_times_a_second():
    from meshpipeline.api.v1 import chat as chat_mod
    assert chat_mod.REQUIREMENT_POLL_S <= 0.25             # before: 1 s (a landed reading waited up to 1 s)


# item 4: a flow route with no checklist before the run - kept, never "couldn't read that"

WALL = {"id": "Face_1", "label": "Face 1 · round Ø40 mm"}
NO_CHECKLIST_CASES = [
    ({"text": "can you make the cells 0.5 mm on this wall?", "faces": [WALL]},
     "Got it, I've kept 0.5 mm on Face 1 (round Ø40 mm) for the mesh."),
    ({"text": "y+ of 1 on the walls please"}, "Got it, I've kept y+ of 1 for the mesh."),
    ({"text": "5 prism layers on the pipe wall"}, "Got it, I've kept 5 prism layers for the mesh."),
    ({"text": "finer around the bend", "attachments": PIC}, "Got it, I've kept that for the mesh."),
]


@pytest.mark.parametrize("view", [None, {"status": "done", "rows": [], "unsure": [], "flow": True,
                                         "notes_only": True}], ids=["none", "notes_only"])
@pytest.mark.parametrize("note,start", NO_CHECKLIST_CASES)
def test_with_no_checklist_a_requirement_is_kept_never_unreadable(note, start, view):
    got = PT.reply_for(note, view)
    assert got.startswith(start), got
    assert PT.NO_CHECKLIST in got and "couldn't" not in got and "applied" not in got.split(PT.NO_CHECKLIST)[0]
    assert ("don't read pictures" in got) == bool(note.get("attachments"))
    f = PR.facts(note, view)
    assert f["kept_for_the_run"] is True
    assert PR.check("I couldn't read that as a mesh requirement.", f) == "a kept requirement not said as kept"
    assert PR.check("Got it, I've kept that for the mesh. I'll read it when the mesh is built.", f) == ""


def test_on_a_parts_checklist_nothing_taken_is_still_said_plainly():
    assert PT.reply_for({"text": "make it look nice"}, _done()) == PT.NOT_READ
    assert PT.reply_for({"text": "make it look nice"}, {**_done(), "notes_only": True}) == PT.NOT_READ


def test_a_flow_session_whose_parts_view_is_none_answers_with_the_kept_line(monkeypatch):
    from test_intake_approval_lifecycle import _approval, _route

    from meshpipeline.agents.intake import message as M
    from meshpipeline.api.v1 import chat as chat_mod
    note = {"text": "y+ of 1 on the walls please", "source": "chat", "turn": 2}

    async def _keep(inbound, db, **_kw):
        return dict(note)
    monkeypatch.setattr(M, "_keep_requirement", _keep)
    monkeypatch.setattr(PM, "finalize", lambda s, owner_id="": None)       # GET .../check/parts says "none"
    monkeypatch.setattr(PM, "view", lambda s, owner_id="": None)
    monkeypatch.setattr(PR, "owed_note", lambda s: dict(note))
    monkeypatch.setattr(PR, "_ask", _fails)                                 # the model down: the plain line

    async def _brief(*_a, **_k):
        return None
    monkeypatch.setattr(chat_mod, "_brief", _brief)
    rec, sel = _approval(msg_count=1)
    resp, calls, repo = _route("y+ of 1 on the walls please", rec, sel)
    assert resp.reply == f"Got it, I've kept y+ of 1 for the mesh. {PT.NO_CHECKLIST}"
    assert "couldn't read" not in resp.reply and calls == {"intake": 0, "dispatch": 0}
    assert repo.append_message.await_args_list[-1][0][2:] == ("assistant", resp.reply)


def test_the_view_says_a_flow_route_and_no_parts(monkeypatch):
    from meshpipeline.application import geometry_check as GC
    store = {PM.MATCHES: {"key": "k", "status": "done", "section": {"status": "none"}}}
    monkeypatch.setattr(GC, "read_check", lambda sid: {"status": "done"})
    monkeypatch.setattr(PM, "parts_seen", lambda check: None)
    monkeypatch.setattr(PM, "flow_route", lambda check, session: True)
    monkeypatch.setattr(PM, "key_of", lambda notes, table: "k")
    monkeypatch.setattr(PM, "_read", lambda sid, name: store.get(name))
    monkeypatch.setattr(PM, "notes_lines", lambda stored, **_k: [])
    session = SimpleNamespace(id="s1", user_notes=[{"text": "y+ of 1 on the walls please", "source": "chat",
                                                    "turn": 1, "id": "n1"}], request_txt="", messages=[])
    got = PM.view(session)
    assert got["status"] == "done" and got["flow"] is True and got["notes_only"] is True
