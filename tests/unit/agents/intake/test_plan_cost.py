# Responsibility: Verify NOTE-26: what a typed requirement does to an FEA solid's plan is said at the plan step - the
#       cost kept with the checklist's reading (application/plan_cost, from engines/fea_plan_options.requirements_cost),
#       said only when worth saying, written by the model from facts that hold it under the guard, with a code-written
#       fallback sentence, and the count row showing the new estimate.
# Boundaries: pure over views and costs given as data, plus one planning of a small filleted bracket built in the test
#       (tests/fea_fixtures.py); no model call, no mesher, no object store.
from __future__ import annotations

import asyncio

import pytest

from meshpipeline.agents.intake import plan_reply as PR
from meshpipeline.agents.intake import plan_talk as PT
from meshpipeline.application import part_matches as PM
from meshpipeline.application import plan_cost as PC
from meshpipeline.contracts import user_notes as UN
from meshpipeline.engines import fea_ai
from meshpipeline.engines import mesh_requirements as MR


def _cost(before=700_000, after=2_900_000, tb=8, ta=25, bigger=False):
    return {"rule": "NOTE-26", "before": {"elements": before, "minutes": tb + 4, "usual_minutes": tb, "standard": True},
            "after": {"elements": after, "minutes": ta + 9, "usual_minutes": ta, "standard": not bigger},
            "factor": round(after / before, 2), "bigger_machine": bigger}


def _notes(*texts):
    return UN.clean([UN.make(t, source=UN.SOURCE_CHAT, turn=i + 1) for i, t in enumerate(texts)])


def _fillet_view(notes, cost):
    nid = str(UN.own_notes(notes)[-1]["id"])
    row = {"note": nid, "quote": "finer at the fillets", "words": "finer at the fillets", "size": "finer",
           "parts": ["Feature_1"], "parts_text": "the fillets"}
    return PM.with_cost({"status": "done", "rows": [row], "unsure": []}, cost), nid


def test_the_rule_id_is_cited():
    assert PC.RULE == "NOTE-26" and PT.COST_RULE == "NOTE-26"


@pytest.mark.parametrize("before,after,bigger,said", [
    (700_000, 2_900_000, False, True),       # four times: said
    (1_000_000, 1_300_000, False, True),     # just past a quarter
    (1_000_000, 1_200_000, False, False),    # inside the estimate's own error
    (1_000_000, 700_000, False, True),       # coarser by more than a quarter
    (1_000_000, 1_100_000, True, True),      # a bigger machine is always said
])
def test_it_is_said_only_when_worth_saying(before, after, bigger, said):
    assert PC.worth_saying(_cost(before, after, bigger=bigger)) is said
    assert not PC.worth_saying(None) and not PC.worth_saying({"before": {"elements": 0}, "after": {"elements": 5}})


def test_the_plain_sentence_says_the_elements_before_and_after_and_no_minutes():
    # no minutes: the time models held do not cover the whole wait (the C5 knuckle: "3 minutes", 8.5 taken)
    assert PT.cost_line(_cost()) == "That takes it from about 0.7 million to about 2.9 million elements."
    assert PT.cost_line(_cost(1_000_000, 10_400_000, 9, 96, bigger=True)) == (
        "That takes it from about 1.0 million to about 10 million elements, and it needs a bigger machine.")
    assert PT.cost_line(_cost(9_600, 27_000, 1, 1)) == "That takes it from about 9,600 to about 27,000 elements."


def test_the_reply_never_carries_the_cost():
    """The C5 knuckle, 26 Nov: replies of 47, 31 and 24 s while the cost was planned. The reply says what was set;
    the count is its own line after it (count_owed)."""
    notes = _notes("finer at the fillets")
    view, nid = _fillet_view(notes, _cost())
    assert "That takes it" not in PT.reply_for(notes[-1], view, note_id=nid)
    assert "what_it_costs" not in PR.facts(notes[-1], view, note_id=nid)


def test_the_count_line_is_floor_worded_when_the_ai_step_gave_no_decision():
    assert PT.cost_line({**_cost(), "floor": True}) == (
        "That takes it from about 0.7 million to at least about 2.9 million elements.")


class _Session:
    def __init__(self, notes, messages, job_id=None):
        self.id, self.user_notes, self.messages, self.job_id = "s1", notes, messages, job_id
        self.intake_gate = {}


class _Repo:
    def __init__(self, session):
        self.session, self.appended = session, []

    async def get_for_owner(self, db, sid, owner, organization_id=""):
        return self.session

    async def append_message(self, db, sid, role, text):
        self.appended.append((role, text))
        self.session.messages.append({"role": role, "content": text})


class _Db:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def commit(self):
        pass


def test_the_count_line_follows_the_reply_once_and_only_for_the_notes_as_they_stand(monkeypatch):
    notes = _notes("finer at the fillets")
    view, nid = _fillet_view(notes, _cost())
    session = _Session(notes, [{"role": "user", "content": "finer at the fillets"},
                               {"role": "assistant", "content": "I've set a finer mesh at the fillets."}])
    repo = _Repo(session)
    said: list = []
    owed = {"key": "k1", "cost": view["cost"]}
    monkeypatch.setattr(PM, "count_owed", lambda s: None if said else owed)
    monkeypatch.setattr(PM, "view", lambda s, owner_id="": view)
    monkeypatch.setattr(PM, "count_said", lambda sid, key: said.append(key))
    monkeypatch.setattr(PR, "owed_note", lambda s: None)

    def run():
        return asyncio.run(PR.count_owed(session, owner_id="u1", session_repo=repo, db_factory=_Db))
    assert run() == "That takes it from about 0.7 million to about 2.9 million elements."
    assert repo.appended == [("assistant", "That takes it from about 0.7 million to about 2.9 million elements.")]
    assert said == ["k1"] and run() == ""                      # once
    # the reply to the newest requirement is still owed: the reply comes first
    said.clear()
    monkeypatch.setattr(PR, "owed_note", lambda s: {"text": "x"})
    assert run() == ""
    # too small to say: nothing said, and never asked again
    monkeypatch.setattr(PR, "owed_note", lambda s: None)
    small, _ = _fillet_view(notes, _cost(1_000_000, 1_100_000))
    monkeypatch.setattr(PM, "view", lambda s, owner_id="": small)
    owed["cost"] = small["cost"]
    assert run() == "" and said == ["k1"]


def test_the_count_row_shows_the_new_estimate(monkeypatch):
    monkeypatch.setattr(fea_ai, "FEA_AI", True)
    notes = _notes("keep it under 1.5 million elements")
    got = PM.with_counts({"status": "done", "rows": [], "unsure": []}, notes, flow=False)
    got = PM.with_cost(got, _cost(700_000, 1_400_000))
    (row,) = got["rows"]
    assert row["size"] == "at most 1.5 million elements" and row["estimate"] == "Planned: about 1.4 million elements"
    assert PM.with_cost(got, None) is got


def test_a_count_becomes_the_runs_target():
    rows = PT.count_rows({"text": "keep it under 1.5 million elements"}, flow=False)
    assert PC.target_of(rows) == {"elements": 1_500_000, "mode": "at_most", "quote": "", "source": "notes"}
    assert PC.target_of([]) is None
    assert PC.wanted(None, rows) and not PC.wanted({"status": "none"}, [])
    # a requirement only the notes' own reading holds (a size on a picked face) is costed too; the standard's alone is not
    req = {**MR.empty(), "rows": [{"id": "r1", "field": "local", "from": "notes"}],
           "local": [{"id": "r1", "what": "size", "mm": 2.0, "faces": ["Face_6"]}]}
    assert PC.wanted({"status": "none"}, [], MR.section("used", requirements=req))
    std = {**req, "rows": [{"id": "s1", "field": "local", "from": "settings"}]}
    assert not PC.wanted({"status": "none"}, [], MR.section("used", requirements=std))
    assert PC.wanted({"status": "used", "entries": [{"ask": "refine"}]}, [])


def test_nothing_is_costed_without_the_stages_sized_plan(monkeypatch):
    from meshpipeline.application import geometry_check as GC
    monkeypatch.setattr(GC, "read_check", lambda sid: {"source": {"x": 1}, "facts": {}})
    called = []
    monkeypatch.setattr(PC, "_measured", lambda *a, **k: called.append(1))
    section = {"status": "used", "entries": [{"ask": "refine", "parts": ["Feature_1"]}]}
    assert PC.for_reading("s1", section, []) is None and not called
    # and nothing to cost: not even the check is read - the reading keeps a marker, so the next one knows the plan
    # before it was the team's standard alone
    monkeypatch.setattr(GC, "read_check", lambda sid: pytest.fail("read"))
    assert PC.for_reading("s1", {"status": "none"}, []) == {"rule": "NOTE-26", "status": PC.NOTHING}
