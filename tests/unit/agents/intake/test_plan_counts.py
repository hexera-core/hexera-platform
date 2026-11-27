# Responsibility: Verify NOTE-24: an element or cell count typed at the plan step ("keep it under 1.5 million
#       elements") is a row of the checklist on the solid, shell and flow meshes, the reply says it plainly (model-written
#       under the guard, with a code fallback), a clash with typed sizes is told without promising the count, a count
#       question stays a question and a size stays a size.
from __future__ import annotations

import asyncio

import pytest

from meshpipeline.agents.intake import plan_reply as PR
from meshpipeline.agents.intake import plan_talk as PT
from meshpipeline.application import part_matches as PM
from meshpipeline.contracts import user_notes as UN
from meshpipeline.engines import fea_ai

#: (as typed, the bound, the whole number, the number as typed)
PHRASES = [
    ("keep it under 1.5 million elements", "ceiling", 1_500_000, "1.5 million"),
    ("about 800k elements", "target", 800_000, "800k"),
    ("at least 2 million cells", "floor", 2_000_000, "2 million"),
    ("max 500,000 elements", "ceiling", 500_000, "500,000"),
]
#: the routes at the plan step: the solid and the shell mesh share the FEA checklist; the flow mesh says "cells"
ROUTES = [("fea solid", False, "elements"), ("fea shell", False, "elements"), ("flow", True, "cells")]


def test_the_rule_id_is_cited():
    assert PT.COUNT_RULE == "NOTE-24" and PM.COUNT_RULE == "NOTE-24"


@pytest.mark.parametrize("text,what,value,typed", PHRASES)
def test_a_count_in_the_words_is_read_with_its_bound_and_its_number_as_typed(text, what, value, typed):
    got = PT.count_asks(text)
    assert [(c["what"], c["value"], c["typed"]) for c in got] == [(what, value, typed)]


@pytest.mark.parametrize("text", ["3 mm elements", "2 elements through the wall", "700k-1.5M elements",
                                  "between 1 and 2 million elements", "how many elements will this be?"])
def test_a_size_a_small_number_a_range_and_a_question_are_no_count(text):
    assert PT.count_asks(text) == []


def test_a_count_question_stays_a_question_and_a_size_stays_a_size():
    assert PT.reading("how many elements will this be?") == PT.QUESTION
    assert PT.reading("3 mm elements") == PT.NOTE and PT.count_asks("3 mm elements") == []


def _notes(*texts):
    return UN.clean([UN.make(t, source=UN.SOURCE_CHAT, turn=i + 1) for i, t in enumerate(texts)])


def _view(notes, *, flow, rows=()):
    got = PM.with_counts({"status": "done", "rows": list(rows), "unsure": []}, notes, flow=flow)
    return {**got, **({"flow": True} if flow else {})}


@pytest.mark.parametrize("route,flow,noun", ROUTES)
@pytest.mark.parametrize("text,what,value,typed", PHRASES)
def test_a_count_is_a_checklist_row_and_the_reply_says_it(monkeypatch, route, flow, noun, text, what, value, typed):
    monkeypatch.setattr(fea_ai, "FEA_AI", True)
    notes = _notes(text)
    view = _view(notes, flow=flow)
    nid = str(UN.own_notes(notes)[-1]["id"])
    (row,) = view["rows"]
    assert row["note"] == nid and row["parts_text"] == "the whole mesh" and row["count"]["value"] == value
    assert row["size"] == f"{PT._C_CARD[what]} {typed} {noun}"
    plain = PT.reply_for(notes[-1], view, note_id=nid)
    assert plain == {"ceiling": f"I'll keep it under {typed} {noun}.", "floor": f"I'll make it at least {typed} {noun}.",
                     "target": f"I'll aim for about {typed} {noun}."}[what], route
    assert PT.NOT_READ not in plain and "couldn't" not in plain


def test_the_newest_note_that_states_a_count_gives_the_row(monkeypatch):
    monkeypatch.setattr(fea_ai, "FEA_AI", True)
    view = _view(_notes("about 800k elements", "finer at the holes", "max 500,000 elements"), flow=False)
    assert [r["size"] for r in view["rows"]] == ["at most 500,000 elements"]


def test_with_the_fea_ai_step_off_no_count_row_is_shown_where_the_run_would_not_apply_it(monkeypatch):
    monkeypatch.setattr(fea_ai, "FEA_AI", False)
    notes = _notes("keep it under 1.5 million elements")
    assert _view(notes, flow=False)["rows"] == []
    assert len(_view(notes, flow=True)["rows"]) == 1            # the flow reader applies it on every flow mesh


def _sized(nid="n0"):
    return {"note": nid, "quote": "use 3 mm everywhere", "words": "use 3 mm everywhere", "size": "3 mm",
            "parts": [], "parts_text": "the whole model"}


@pytest.mark.parametrize("flow,wins", [(False, "your sizes win"), (True, "the count comes first")])
def test_a_count_beside_a_typed_size_is_told_which_wins_and_never_promised(monkeypatch, flow, wins):
    monkeypatch.setattr(fea_ai, "FEA_AI", True)
    notes = _notes("use 3 mm everywhere", "keep it under 1.5 million elements")
    nid = str(UN.own_notes(notes)[-1]["id"])
    view = _view(notes, flow=flow, rows=[_sized(str(UN.own_notes(notes)[0]["id"]))])
    plain = PT.reply_for(notes[-1], view, note_id=nid)
    assert plain.startswith("I'll keep it under 1.5 million") and wins in plain and "result will say" in plain
    f = PR.facts(notes[-1], view, note_id=nid)
    assert f["count_may_not_hold"] and f["already_on_the_checklist"][0]["what"] == "3 mm elements"
    assert PR.check("Sure, I'll keep it under 1.5 million elements, guaranteed.", f).startswith(
        "a count that may not hold said as sure")
    assert PR.check("Sure, I'll keep it under 1.5 million elements.", f).startswith(
        "a count that may not hold said as sure")
    assert PR.check("Okay, I'll keep it under 1.5 million elements. Your 3 mm size comes first, so if both can't "
                    "fit, the result will say so.", f) == ""


def _count_facts(text="keep it under 1.5 million elements"):
    notes = _notes(text)
    nid = str(UN.own_notes(notes)[-1]["id"])
    view = _view(notes, flow=False)
    return notes[-1], view, nid, PR.facts(notes[-1], view, note_id=nid)


def test_the_guard_takes_a_natural_count_reply_and_refuses_a_wrong_one(monkeypatch):
    monkeypatch.setattr(fea_ai, "FEA_AI", True)
    _note, _view_, _nid, f = _count_facts()
    assert f["applied"][0]["count"] == {"bound": "at most", "number_as_typed": "1.5 million", "noun": "elements"}
    assert PR.check("Will do, I'll keep the mesh under 1.5 million elements.", f) == ""
    assert PR.check("Will do, at least 1.5 million elements.", f).startswith("a count said with another bound")
    assert PR.check("Will do, I'll keep the count down.", f).startswith("the count not said as typed")
    assert PR.check("Will do, I'll keep it under 2 million elements.", f).startswith("a number not in the facts")


def test_the_reply_is_the_models_when_the_guard_takes_it_and_the_plain_line_when_not(monkeypatch):
    monkeypatch.setattr(fea_ai, "FEA_AI", True)
    note, view, nid, _f = _count_facts()

    def run(reply):
        async def model(messages, *, job_id, user_id):
            assert "count" in messages[0]["content"]
            return reply
        monkeypatch.setattr(PR, "_ask", model)
        return asyncio.run(PR.written(note, view, note_id=nid))
    good = run("Sounds good, I'll keep it under 1.5 million elements.")
    assert good["source"] == PR.SOURCE_MODEL and good["text"].endswith("under 1.5 million elements.")
    bad = run("I couldn't find a mesh setting in that.")
    assert bad["source"] == PR.SOURCE_FALLBACK and bad["text"] == "I'll keep it under 1.5 million elements."
