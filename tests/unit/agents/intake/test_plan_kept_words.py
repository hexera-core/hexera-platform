# Responsibility: Verify NOTE-25: a statement at the plan step the checklist takes nothing from is kept in the user's
#       notes for the run and answered as kept - model-written under the guard, never "couldn't find a mesh setting",
#       never said as set - a change to the CAD itself is told that meshing cannot make it, and questions are unchanged.
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from meshpipeline.agents.intake import approval as ap
from meshpipeline.agents.intake import message as M
from meshpipeline.agents.intake import plan_reply as PR
from meshpipeline.agents.intake import plan_talk as PT
from meshpipeline.contracts import user_notes as UN

STATEMENTS = ["make it look nice", "it's for a stress analysis of the bracket", "the part is cast aluminium"]


def _done(rows=(), unsure=()):
    return {"status": "done", "rows": list(rows), "unsure": list(unsure)}


def test_the_rule_id_is_cited():
    assert PT.KEPT_RULE == "NOTE-25"


def test_a_statement_kept_at_the_plan_step_is_in_the_users_notes_and_in_the_runs_notes_file(tmp_path):
    # NOTE-18 keeps it before it is read; NOTE-2 hands every route's planning step the notes file
    kept = {}

    async def set_user_notes(_db, _sid, notes):
        kept["notes"] = UN.merge(kept.get("notes"), notes)
    repo = SimpleNamespace(set_user_notes=set_user_notes)
    inbound = SimpleNamespace(content="make it look nice", session_id=uuid.uuid4())
    asyncio.run(M._keep_requirement(inbound, None, locked=None, messages=[{"role": "user", "content": "make it look nice"}],
                                    session_repo=repo))
    assert [n["text"] for n in UN.own_notes(kept["notes"])] == ["make it look nice"]
    assert UN.write_file(tmp_path, kept["notes"]) and "make it look nice" in (tmp_path / UN.NOTES_FILE).read_text()
    assert "make it look nice" in UN.prompt_block(kept["notes"], consumer="fea_ai")


@pytest.mark.parametrize("text", STATEMENTS)
def test_words_that_set_nothing_are_said_kept_never_turned_away(text):
    note = {"text": text}
    plain = PT.reply_for(note, _done())
    assert plain == PT.NOT_READ and "kept your words" in plain and "mesh setting" not in plain
    assert "couldn't find" not in plain and "size" in plain and "element count" in plain
    f = PR.facts(note, _done())
    assert f["kept_for_planning"]["what_happens"].startswith("the mesh planning step reads their words as guidance")
    assert "applied" not in f or not f["applied"]


def test_a_change_to_the_cad_itself_is_told_meshing_cannot_make_it():
    note = {"text": "remove the small holes"}
    plain = PT.reply_for(note, _done())
    assert plain == PT.NOT_BY_MESHING and "can't" in plain and "kept" in plain
    assert "not_by_meshing" in PR.facts(note, _done()) and "kept_for_planning" not in PR.facts(note, _done())


GOOD = ("That's not something I can put on the checklist, but I've kept your words for the mesh planning step, which "
        "reads them as guidance, and the result will say what came of them.")


@pytest.mark.parametrize("reply,why", [
    ("Got it, I've set that for you.", "claims something was applied when nothing was"),
    ("Sure, that's applied across the whole mesh.", "claims something was applied when nothing was"),
    ("That's guaranteed in the planning step.", "claims something was applied when nothing was"),
    ("I couldn't find a mesh setting in that, but I've kept it.", "our jargon"),
    ("Okay, the planner will look at that.", "a kept note not said as kept"),
])
def test_the_guard_refuses_a_kept_note_said_as_applied_or_not_said_as_kept(reply, why):
    f = PR.facts({"text": "make it look nice"}, _done())
    assert PR.check(GOOD, f) == ""
    assert PR.check(reply, f).startswith(why)


def test_the_guard_takes_a_negated_applied_for_a_cad_change():
    f = PR.facts({"text": "remove the small holes"}, _done())
    assert PR.check("Removing holes is a change to the CAD itself, which meshing can't do. I've kept your note, and "
                    "the result will say it wasn't applied.", f) == ""


def test_the_reply_is_the_models_under_the_guard_with_the_kept_line_as_its_fallback(monkeypatch):
    note = {"text": "make it look nice"}

    def run(reply):
        async def model(messages, *, job_id, user_id):
            assert "kept_for_planning" in messages[1]["content"] and "mesh planning step" in messages[0]["content"]
            return reply
        monkeypatch.setattr(PR, "_ask", model)
        return asyncio.run(PR.written(note, _done()))
    assert run(GOOD)["source"] == PR.SOURCE_MODEL
    bad = run("I couldn't find a mesh setting in that.")
    assert bad["source"] == PR.SOURCE_FALLBACK and bad["text"] == PT.NOT_READ


def test_a_question_that_takes_nothing_is_still_answered_as_a_question():
    note = {"text": "is that going to be fine for a stress run"}
    assert PT.took_nothing(note, _done()) and PR.asked_not_set(note, _done())
    assert not PR.asked_not_set({"text": "make it look nice"}, _done())


def test_the_one_place_asks_keep_their_exact_lines():
    u = {"note": "n1", "quote": "make it finer in this hole", "words": "make it finer in this hole", "size": "finer",
         "which_one": 11, "noun": "hole"}
    got = PT.reply_for({"text": "make it finer in this hole"}, _done(unsure=[u]), note_id="n1")
    assert got == PT.which_one_line(u) and "kept_for_planning" not in PR.facts(
        {"text": "make it finer in this hole"}, _done(unsure=[u]), note_id="n1")


# the route: a statement at the plan step is kept and answered with the kept line, never turned away

def test_the_route_keeps_a_statement_and_answers_it_as_kept(monkeypatch):
    sid = uuid.uuid4()
    note = UN.make("make it look nice", source=UN.SOURCE_CHAT, turn=2)
    session = SimpleNamespace(id=sid, job_id=None, intake_gate={"approval": {"state": "live"}},
                              messages=[{"role": "user", "content": "x"}, {"role": "assistant", "content": "plan"},
                                        {"role": "user", "content": "make it look nice"}],
                              user_notes=[note])
    monkeypatch.setattr(ap, "is_live", lambda _a: True)
    repo = MagicMock()
    repo.get_for_owner = AsyncMock(return_value=session)
    repo.append_message = AsyncMock()

    @asynccontextmanager
    async def db_factory():
        yield MagicMock(commit=AsyncMock())

    async def model(messages, *, job_id, user_id):
        raise TimeoutError
    monkeypatch.setattr(PR, "_ask", model)
    said = asyncio.run(PR.answer_owed(session, _done(), owner_id="u", session_repo=repo, db_factory=db_factory))
    assert said == PT.NOT_READ and "couldn't find a mesh setting" not in said
    assert repo.append_message.await_args.args[-1] == PT.NOT_READ
