# Responsibility: Verify NOTE-20: a question about the mesh at the plan step - typos and no question mark included - is
#       read as a question (in code, and from the model's "requirement" when the words ask), is never answered "I
#       couldn't find a mesh setting in that", keeps nothing and starts nothing, and is answered from the numbers the
#       product holds before the run (the check's planned sizes, the checklist's rows) under a guard that refuses any
#       number not in those facts. The stand-in model cannot judge tone: routing, facts and the guard are tested.
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from meshpipeline.agents.intake import consent as C
from meshpipeline.agents.intake import plan_go as PG
from meshpipeline.agents.intake import plan_reply as PR
from meshpipeline.agents.intake import plan_talk as PT
from meshpipeline.application import fea_check as FC
from meshpipeline.application import part_matches as PM

#: as typed on the deployed site (3-part FEA assembly, 3 requirements set)
ASKED = "so for this mesh basedon my rquirements, what would the elemwnt size be roughly"
SIX = [{"id": f"Face_{k}", "label": f"Face {k} · round Ø8 mm"} for k in range(1, 7)]
ROWS = [{"note": "n1", "quote": "finer on these", "size": "finer", "faces": SIX, "picked": True, "parts": []},
        {"note": "n2", "quote": "2 mm on part 2", "size": "2 mm", "parts": ["Part_2"], "parts_text": "part 2"},
        {"note": "n3", "quote": "finer in the bore", "size": "finer", "parts": ["Feature_4"],
         "parts_text": "the bore (12 mm across)"}]
STEP = {"finer": 0.5, "coarser": 2.0}
PLAN = {"known": True, "step": STEP, "coarsest_mm": 4.1, "finest_mm": 0.62, "finest_where": "the fillets",
        "elements": "about 412,000 elements", "before_requirements": True}
NOT_YET = {"known": False, "step": STEP, "why": FC.SIZES_AT_RUN}


def _done(rows=(), unsure=()):
    return {"status": "done", "rows": list(rows), "unsure": list(unsure)}


def _answers(label):
    calls = []

    async def ask(messages, *, job_id, user_id):
        calls.append(messages)
        return label
    return ask, calls


def _read(text, **kw):
    return asyncio.run(PG.read(text, consent=C.reading(text), **kw))


def test_the_rule_ids_are_cited():
    assert PT.QUESTION_RULE == "NOTE-20" and PR.QUESTION_RULE == "NOTE-20"


# the reading

@pytest.mark.parametrize("text", [
    ASKED,
    "what would the elemwnt size be roughly",
    "how many elements will it be",
    "how long will it tak",
    "what did i set so far",
    "what will the cell count be?",
    "tell me the element size",
    "roughly how fine will the mesh be on the fillets",
    "will it be finer at the bore",
])
def test_a_question_about_the_mesh_is_a_question_in_code_and_no_model_is_asked(text):
    ask, calls = _answers("requirement")
    got = _read(text, ask_fn=ask)
    assert got.reading == PT.QUESTION and got.by == PG.BY_CODE, text
    assert calls == [] and PT.reading(text) == PT.QUESTION


@pytest.mark.parametrize("text", [
    "go finer on the bore", "can you make it finer at the bore?", "how about finer at the bore",
    "make it finer in this hole", "will you make the fillets finer", "proceed with 2 mm on the holes",
    "would 2 mm on the bore be ok?",
])
def test_words_that_ask_for_a_setting_stay_a_requirement(text):
    assert PT.reading(text) == PT.NOTE, text


def test_a_question_about_the_kind_of_mesh_stays_the_intakes():
    # no number answers "what element type": the intake, which holds the plan, answers it as before
    for text in ("what element type will it use?", "which engine will mesh it", "why is the size so small"):
        assert PT.reading(text) == PT.INTAKE, text


def test_the_models_requirement_for_words_that_ask_is_answered_as_a_question():
    # words with no mesh word in them reach the model: its "requirement" is not taken when the words ask
    ask, _ = _answers("requirement")
    assert _read("what would it be roughly", ask_fn=ask).reading == PT.QUESTION
    ask, _ = _answers("question")
    assert _read("hows the count looking", ask_fn=ask).reading == PT.QUESTION       # about the mesh: ours
    ask, _ = _answers("question")
    assert _read("all right then", ask_fn=ask).reading == PT.INTAKE                 # any other question: the intake's


# the facts and the guard

def test_the_facts_hold_the_plans_numbers_and_each_requirements_size():
    f = PR.question_facts(ASKED, _done(ROWS), PLAN)
    assert f["plan"]["coarsest_mm"] == 4.1 and f["plan"]["finest_mm"] == 0.62
    assert f["plan"]["elements"] == "about 412,000 elements" and "before your requirements" in f["plan"]["planned"]
    picked, typed, bore = f["your_requirements"]
    assert picked["where"] == "the 6 faces you picked" and "x0.5" in picked["size_there"] \
        and "settled when the mesh is built" in picked["size_there"]
    assert typed["size_there"].startswith("2 mm") and "part 2" in typed["where"]
    assert "x0.5" in bore["size_there"]
    plain = f["plain_version_that_is_exactly_right"]
    assert "4.1 mm" in plain and "0.62 mm" in plain and PT.NOT_READ not in plain
    assert PR.check_answer(plain, f) == ""


def test_with_no_planned_numbers_the_answer_says_what_is_not_known_and_when():
    f = PR.question_facts(ASKED, _done(ROWS[:1]), NOT_YET)
    assert f["plan"] == {} and f["not_known_yet"] == [FC.SIZES_AT_RUN]
    assert "when the mesh is built" in FC.SIZES_AT_RUN and "once the run starts" in FC.SIZES_AT_RUN
    assert "mm" not in f["plain_version_that_is_exactly_right"].split("Your requirements")[0]


@pytest.mark.parametrize("reply,why", [
    ("Roughly 3 mm over most of the part.", "a number not in the facts"),             # 3 is in no fact
    ("About 2.05 mm on the 6 faces you picked.", "a number not in the facts"),        # worked out: 4.1 x 0.5
    ("It'll be about a million elements.", "a number not in the facts"),
    ("Roughly 4.1 mm, and about 900,000 elements.", "a number not in the facts"),
    ("Great question! Roughly 4.1 mm.", "an exclamation, filler or emoji"),
    ("Roughly 4.1 mm (NOTE-20).", "a rule id"),
])
def test_the_guard_refuses_a_figure_the_facts_do_not_hold(reply, why):
    assert PR.check_answer(reply, PR.question_facts(ASKED, _done(ROWS), PLAN)).startswith(why), reply


def test_the_guard_passes_an_answer_from_the_facts_and_refuses_a_count_when_none_is_held():
    f = PR.question_facts(ASKED, _done(ROWS), PLAN)
    ok = ("Roughly 4.1 mm over most of the part, down to 0.62 mm at the fillets. On the 6 faces you picked it'll "
          "be about half that, and the same in the bore.")
    assert PR.check_answer(ok, f) == ""
    none = PR.question_facts(ASKED, _done(ROWS), NOT_YET)
    assert PR.check_answer("Roughly 4.1 mm over most of the part.", none).startswith("a number not in the facts")
    assert PR.check_answer("About 412,000 elements.", none).startswith("a number not in the facts")
    assert PR.check_answer("I've set 2 mm elements everywhere.", PR.question_facts("how fine will it be", _done(),
                                                                               NOT_YET)) != ""


def test_a_refused_answer_gives_the_plain_one_and_a_good_one_is_said():
    bad, _ = _answers("Roughly 3 mm over most of the part.")
    got = asyncio.run(PR.answered(ASKED, _done(ROWS), PLAN, ask_fn=bad))
    assert got["source"] == PR.SOURCE_FALLBACK and got["why"].startswith("a number not in the facts")
    assert "4.1 mm" in got["text"] and PT.NOT_READ not in got["text"]
    good, calls = _answers("Roughly 4.1 mm over most of the part; the 6 faces you picked get about half that.")
    got = asyncio.run(PR.answered(ASKED, _done(ROWS), PLAN, ask_fn=good))
    assert got["source"] == PR.SOURCE_MODEL
    assert calls[0][0]["content"] == PR.compose_answer_system()
    assert [b[0] for b in PR.PLAN_ANSWER_PROMPT_BLOCKS] and all(p and callable(b) for _, p, b in
                                                                PR.PLAN_ANSWER_PROMPT_BLOCKS)


# the planned numbers, where the product already holds them

def test_the_planned_sizes_are_the_checks_own_and_absent_ones_say_when(monkeypatch):
    sized = {FC.PLAN_FACT_KEY: {"sized": True, "recommended": "full",
                                "full": {"elements": 412_345, "minutes": 7, "usual_minutes": 3}}}
    plans = {"full": {"base_size_m": 0.0041, "zones": [
        {"kind": "part", "size_m": 0.002}, {"kind": "part", "size_m": 0.0081},
        {"kind": "fillet", "size_m": 0.00062}, {"kind": "wall", "size_m": 0.0009}]}}
    monkeypatch.setattr(FC, "_kept_plans", lambda sid: plans)
    monkeypatch.setattr(FC, "confirmed_plan_choice", lambda sid: "")
    got = FC.planned_sizes("s1", sized)
    assert got["known"] and got["coarsest_mm"] == 4.1 and got["parts_mm"] == [2.0, 8.1]
    assert got["finest_mm"] == 0.62 and got["finest_where"] == "the fillets"
    assert got["elements"] == "about 412,345 elements" and got["step"] == {"finer": 0.5, "coarser": 2.0}
    assert FC.planned_sizes("s1", {FC.PLAN_FACT_KEY: {"sizing": True}})["why"] == FC.SIZES_BEING_PLANNED
    absent = FC.planned_sizes("s1", {})
    assert absent["known"] is False and absent["why"] == FC.SIZES_AT_RUN and "coarsest_mm" not in absent


# never "I couldn't find a mesh setting in that" for words that ask

def _session(messages, notes):
    return SimpleNamespace(id=uuid.UUID("bbbbcccc-2222-4222-b222-bbbbbbbbbbbb"), job_id=None, messages=messages,
                           user_notes=notes, intake_gate={"approval": {"status": "awaiting", "expires_at": 4e9}})


def test_a_kept_message_that_asks_and_set_nothing_is_answered_as_a_question(monkeypatch):
    from meshpipeline.agents.intake import approval as ap
    monkeypatch.setattr(ap, "is_live", lambda a: bool(a))
    monkeypatch.setattr(FC, "planned_sizes", lambda sid: PLAN)
    monkeypatch.setattr(PR, "_ask", _answers("Roughly 3 mm.")[0])            # refused: the plain answer is said
    text = "what's the element size going to be, with these requirements"
    msgs = [{"role": "assistant", "content": "Here's the plan."}, {"role": "user", "content": text}]
    notes = [{"text": text, "source": "chat", "turn": 1}]
    s = _session(msgs, notes)
    assert PT.reply_for(notes[0], _done(), note_id="n1") == PT.NOT_READ_MESH     # what was said on d9d6183b
    repo = MagicMock()
    repo.get_for_owner = AsyncMock(return_value=s)
    repo.append_message = AsyncMock(side_effect=lambda db, sid, role, t: msgs.append({"role": role, "content": t}))

    @asynccontextmanager
    async def _db():
        yield AsyncMock()

    async def _w(*_a, **_k):
        raise AssertionError("a question is not answered as a requirement")
    said = asyncio.run(PR.answer_owed(s, _done(), owner_id="alice", session_repo=repo, db_factory=_db, write_fn=_w))
    assert said and PT.NOT_READ not in said and "4.1 mm" in said


# the route: nothing kept, nothing started, answered from the facts

def test_the_question_as_typed_is_answered_from_the_facts_through_the_route(monkeypatch):
    from test_intake_approval_lifecycle import _approval, _route

    from meshpipeline.agents.intake import message as M
    from meshpipeline.api.v1 import chat as chat_mod
    kept = []

    async def _keep(inbound, db, **_kw):
        kept.append(inbound.content)
        return {"text": inbound.content}
    monkeypatch.setattr(M, "_keep_requirement", _keep)
    monkeypatch.setattr(PM, "finalize", lambda s, owner_id="": _done(ROWS))
    monkeypatch.setattr(PM, "view", lambda s, owner_id="": _done(ROWS))
    monkeypatch.setattr(FC, "planned_sizes", lambda sid: PLAN)

    async def _brief(*_a, **_k):
        return None
    monkeypatch.setattr(chat_mod, "_brief", _brief)
    asked = []

    async def _model(messages, *, job_id, user_id):
        asked.append(messages)
        return "Roughly 4.1 mm over most of it; the 6 faces you picked get about half that."
    monkeypatch.setattr(PR, "_ask", _model)
    rec, sel = _approval(msg_count=1)
    resp, calls, repo = _route(ASKED, rec, sel)
    assert calls == {"intake": 0, "dispatch": 0} and kept == []
    assert resp.reply == "Roughly 4.1 mm over most of it; the 6 faces you picked get about half that."
    assert resp.awaiting_confirmation is True and PT.NOT_READ not in resp.reply
    assert asked and asked[0][0]["content"] == PR.compose_answer_system()
    assert any(c.args[2:] == ("assistant", resp.reply) for c in repo.append_message.await_args_list)
