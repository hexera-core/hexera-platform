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


def test_the_plain_sentence_says_elements_and_minutes_before_and_after():
    assert PT.cost_line(_cost()) == ("That takes it from about 0.7 million to about 2.9 million elements, so roughly "
                                     "25 minutes instead of 8.")
    assert PT.cost_line(_cost(1_000_000, 10_400_000, 9, 96, bigger=True)) == (
        "That takes it from about 1.0 million to about 10 million elements, so roughly 1.6 hours instead of 9 "
        "minutes, and it needs a bigger machine.")
    assert PT.cost_line(_cost(9_600, 27_000, 1, 1)) == "That takes it from about 9,600 to about 27,000 elements."


def test_the_fallback_reply_carries_the_cost_and_only_for_a_message_that_set_something():
    notes = _notes("finer at the fillets")
    view, nid = _fillet_view(notes, _cost())
    plain = PT.reply_for(notes[-1], view, note_id=nid)
    assert plain.endswith("That takes it from about 0.7 million to about 2.9 million elements, so roughly 25 "
                          "minutes instead of 8.")
    # too small to say: the reply is the reply it always was
    small, _ = _fillet_view(notes, _cost(1_000_000, 1_100_000))
    assert "That takes it" not in PT.reply_for(notes[-1], small, note_id=nid)
    # a later message that set nothing does not say the cost again
    later = _notes("finer at the fillets", "it's cast aluminium")
    view2, _ = _fillet_view(later[:1], _cost())
    nid2 = str(UN.own_notes(later)[-1]["id"])
    assert "That takes it" not in PT.reply_for(later[-1], view2, note_id=nid2)
    assert "what_it_costs" not in PR.facts(later[-1], view2, note_id=nid2)


def test_the_model_is_given_the_cost_and_the_guard_holds_it_to_it():
    notes = _notes("finer at the fillets")
    view, nid = _fillet_view(notes, _cost())
    f = PR.facts(notes[-1], view, note_id=nid)
    assert f["what_it_costs"] == {"elements_before": "about 0.7 million", "elements_after": "about 2.9 million",
                                  "needs_a_bigger_machine": False, "minutes_before": 8, "minutes_after": 25,
                                  "say_it_like": PT.cost_line(_cost())}
    assert "cost" in [name for name, _p, _b in PR.PLAN_REPLY_PROMPT_BLOCKS]
    assert "what_it_costs" in PR.compose_system()
    ok = ("Sure, I took that as the fillets and I'll go finer there. That takes it from about 0.7 to 2.9 million elements, so roughly 25 "
          "minutes instead of 8.")
    assert PR.check(ok, f) == ""
    assert PR.check("Sure, I'll go finer at the fillets.", f) == "the cost not said"
    assert PR.check("Sure, I'll go finer at the fillets. That's about 2.9 million elements on a bigger machine.",
                    f) == "a bigger machine said when none is needed"


def test_the_reply_is_the_models_with_the_cost_or_the_plain_sentence(monkeypatch):
    notes = _notes("finer at the fillets")
    view, nid = _fillet_view(notes, _cost())

    def run(reply):
        async def model(messages, *, job_id, user_id):
            assert "what_it_costs" in messages[1]["content"]
            return reply
        monkeypatch.setattr(PR, "_ask", model)
        return asyncio.run(PR.written(notes[-1], view, note_id=nid))
    good = run("Okay, I took that as the fillets and I'll go finer there. That takes it to about 2.9 million elements from 0.7 million, "
               "roughly 25 minutes instead of 8.")
    assert good["source"] == PR.SOURCE_MODEL
    bad = run("Okay, I'll go finer at the fillets.")
    assert bad["source"] == PR.SOURCE_FALLBACK and bad["text"].endswith("roughly 25 minutes instead of 8.")


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
    assert PC.wanted({"status": "used", "entries": [{"ask": "refine"}]}, [])


def test_nothing_is_costed_without_the_stages_sized_plan(monkeypatch):
    from meshpipeline.application import geometry_check as GC
    monkeypatch.setattr(GC, "read_check", lambda sid: {"source": {"x": 1}, "facts": {}})
    called = []
    monkeypatch.setattr(PC, "_measured", lambda *a, **k: called.append(1))
    section = {"status": "used", "entries": [{"ask": "refine", "parts": ["Feature_1"]}]}
    assert PC.for_reading("s1", section, []) is None and not called
    # and nothing to cost: not even the check is read
    monkeypatch.setattr(GC, "read_check", lambda sid: pytest.fail("read"))
    assert PC.for_reading("s1", {"status": "none"}, []) is None


def test_the_cost_is_the_runs_own_plan_of_a_real_part():
    pytest.importorskip("gmsh")
    import gmsh

    from meshpipeline.engines import fea_driver as FD
    from tests import fea_fixtures as fx

    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        src = fx.write_step(fx.bracket(4.0), ws / "upload.step")
        gmsh.initialize()
        try:
            gmsh.option.setNumber("General.Terminal", 0)
            gmsh.option.setNumber("Geometry.OCCScaling", 0.001)
            gmsh.model.occ.importShapes(str(src))
            gmsh.option.setNumber("Geometry.OCCScaling", 1.0)
            gmsh.model.occ.synchronize()
            gmsh.write(str(ws / "geometry.step"))
            gmsh.write(str(ws / "geometry.brep"))
        finally:
            gmsh.finalize()
        record, surface, diag = FD.measure_staged(ws)
    before = FD.choose_plan(record, surface, diag)
    spot = next(h for h in record.get("hot_spots") or [] if h.get("type") == "fillet")
    section = {"status": "used", "features": {"Feature_1": list(spot["faces"])},
               "parts": [{"id": "Feature_1", "feature": True, "name": "1 rounded edge (fillet)"}],
               "entries": [{"ask": "refine", "parts": ["Feature_1"], "quote": "finer at the fillets"}]}
    got = PC.costed(record, surface, diag, before=before, section=section)
    from meshpipeline.engines import mesh_requirements as MR
    sizes, _scaled, _controls = FD.requirement_sizes(record, MR.section("none"), section, diag)
    assert got["before"]["elements"] == int(before["estimate"]["elements"])
    assert got["after"]["elements"] == int(FD.choose_plan(record, surface, diag, sizes=sizes)["estimate"]["elements"])
    assert got["rule"] == "NOTE-26" and got["seconds"] < 30
