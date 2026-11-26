# Responsibility: Verify STD-1's count - an element count saved in the team's meshing settings is the default count of every FEA run (engines/fea_driver.element_target -> PLAN-fea-27 on a solid, SHMESH-23 on a shell), with its meaning kept (about / at most / at least / a range), a count in the run's own notes always winning over it, and the plan's words saying where the count came from.
# Boundaries: pure over plain data: the standard's reading is fixed (no model), merged as a run merges it.
from __future__ import annotations

import json

import pytest

from meshpipeline.engines import fea_driver as FD
from meshpipeline.engines import fea_plan as P
from meshpipeline.engines import mesh_requirements as MR
from meshpipeline.engines import meshing_standard as MS


def _answer(counts: list[dict]) -> str:
    return json.dumps({"stated_unit": "none", "sizes": [], "layers": [], "physics": [], "element_kind": [],
                       "limits": [], "other": [], "controls": [], "strictness": [], "local": [], "counts": counts,
                       "applies_to": {"whole": ["any"], "lines": []}})


def _merged(text: str, counts: list[dict], notes: dict | None = None) -> dict:
    return MS.merge(MS.checked(_answer(counts), text, route="fea"), notes or MR.section("none"), route="fea")


@pytest.mark.parametrize(("text", "counts", "want"), [
    ("never more than 2 million elements", [{"what": "ceiling", "value": 2_000_000,
                                             "quote": "never more than 2 million elements"}],
     {"elements": 2_000_000, "mode": "at_most"}),
    ("about 50k elements", [{"what": "target", "value": 50_000, "quote": "about 50k elements"}],
     {"elements": 50_000, "mode": "about"}),
    ("at least 10k elements", [{"what": "floor", "value": 10_000, "quote": "at least 10k elements"}],
     {"elements": 10_000, "mode": "at_least"}),
])
def test_the_teams_count_is_the_default_count_with_its_meaning(text, counts, want):
    merged = _merged(text, counts)
    got = FD.element_target({}, None, merged)
    assert got is not None and {k: got[k] for k in want} == want and got["source"] == "settings"
    # the AI gave no answer and the run has notes: the team's count still applies (it needs no model)
    assert FD.element_target({"user_notes": [{"text": "x"}]}, None, merged)["source"] == "settings"
    # the AI read no count in the notes: the team's
    assert FD.element_target({}, {"status": "used", "decision": {}}, merged)["source"] == "settings"


def test_a_range_keeps_both_ends():
    text = "between 100k and 400k elements"
    merged = _merged(text, [{"what": "floor", "value": 100_000, "quote": text},
                            {"what": "ceiling", "value": 400_000, "quote": text}])
    got = FD.settings_count(merged)
    assert got["mode"] == "range" and (got["low"], got["high"]) == (100_000, 400_000)


def test_the_runs_count_wins():
    text = "never more than 2 million elements"
    std = [{"what": "ceiling", "value": 2_000_000, "quote": text}]
    # the AI read a count from the notes: that one
    ai = {"status": "used", "decision": {"target_elements": 30_000, "target_mode": "about"}}
    assert FD.element_target({}, ai, _merged(text, std))["source"] == "ai"
    # the notes give a count (even of another kind - a target against the standard's ceiling): the standard's
    # count rows give way to it, and nothing of the standard aims the plan
    notes = MS.checked(_answer([{"what": "target", "value": 30_000, "quote": "about 30k elements"}]),
                       "about 30k elements", route="fea")
    merged = _merged(text, std, notes)
    assert FD.settings_count(merged) is None
    assert {r["from"] for r in merged["requirements"]["rows"] if str(r["field"]).startswith("cells:")} == {"notes"}
    assert not any(w.startswith("From your settings") and "elements" in w for w in merged["lines"])
    assert ("Not applied (your settings): at most 2 million elements - your notes give the element count for this "
            "run.") in merged["lines"]


def test_nothing_saved_nothing_changes():
    assert FD.settings_count(MR.section("none")) is None and FD.settings_count(None) is None
    assert FD.element_target({}, None) is None and FD.element_target({}, None, MR.section("none")) is None


def test_the_plan_says_the_count_came_from_the_teams_settings():
    t = {"asked": 2_000_000, "mode": "at_most", "source": "settings", "kept_rules": True, "rules_elements": 40_000}
    assert P.target_words({"target": t}) == [
        "You asked for at most 2M elements (from your team's settings); the rules' own plan, about 40k, already "
        "meets that, so it is meshed as the rules recommend."]
