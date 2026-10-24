# Responsibility: Verify "AI governs, rules guarantee" on the FEA builder: the model's decision is checked against its schema, each choice is made exact by the rules and never goes past their minimums, the user's budget only ever lowers the limit, a missing or wrong answer gives the rules' own plan, the same part and words replay the same decision through the AI decision cache, and a lever is only ever picked from the ladder's own options.
# Boundaries: pure over plain zone lists and a fake model router; one in-process plan on a small built part (skipped without OCP/gmsh). No provider is called.
from __future__ import annotations

import asyncio
import itertools
import json
import math

import pytest

from meshpipeline.contracts import model_inference as llm
from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest
from meshpipeline.engines import fea_ai as A
from meshpipeline.engines import fea_ladder as L
from meshpipeline.engines import fea_plan as P

Q = math.pi / 2.0
DIAG = 0.15                      # a 150 mm part: base size 15 mm, 2% is 3 mm
HOLE = {"id": "H1", "kind": "hole", "faces": ["Face_7"], "size_m": Q * 0.004 / 10, "reach_m": 0.004,
        "rule": "PLAN-fea-4", "spot": "H1", "capped": False, "radius_m": 0.004, "thickness_m": 0.006,
        "wall_elements": 3}
SMALL = {**HOLE, "id": "H2", "faces": ["Face_8"], "radius_m": 0.001, "size_m": Q * 0.001 / 10, "spot": "H2"}
WALL = {"id": "W3", "kind": "wall", "faces": ["Face_3"], "size_m": 0.003, "reach_m": 0.006, "rule": "PLAN-fea-3",
        "thickness_m": 0.006, "capped": False}
ROUND = {"id": "R4", "kind": "round", "faces": ["Face_4"], "size_m": Q * 0.01 / 4, "reach_m": 0.0,
         "rule": "PLAN-fea-7", "radius_m": 0.01, "capped": False}
CORNER = {"id": "C5", "kind": "corner", "faces": ["Face_5", "Face_6"], "size_m": 0.0015, "reach_m": 0.0015,
          "rule": "PLAN-fea-5", "spot": "H5", "capped": True, "thickness_m": 0.006}
ZONES = [HOLE, SMALL, WALL, ROUND, CORNER]
RECORD = {"faces": {f"Face_{i}": {} for i in range(1, 9)}, "bodies": 1,
          "hot_spots": [{"id": "H1", "type": "hole", "shape": "through hole", "radius_mm": 4.0, "at_mm": [10, 0, 0]}],
          "micro": [{"kind": "edge", "size_mm": 0.5, "faces": ["Face_2"]},
                    {"kind": "edge", "size_mm": 0.001, "faces": ["Face_1"]}]}


def _decision(**kw) -> dict:
    d = {"analysis": "static", "matters": "the bolt hole", "zones": {}, "features": {}, "joints": [],
         "budget_elements": 0, "words": ["The bolt hole matters."]}
    d.update(kw)
    return d


def _by_id(zs):
    return {z["id"]: z for z in zs}


# ------------------------------------------------------------------ AI-fea-1/2: the rules make it exact ----
def test_refine_a_hot_spot_is_20_per_quarter_turn_and_4_through_its_wall_never_finer_than_half():
    zs, applied = A.apply_zones(ZONES, _decision(zones={"H1": {"priority": "refine", "reason": "user named it"}}),
                                RECORD, diag_m=DIAG)
    h = _by_id(zs)["H1"]
    assert h["size_m"] == pytest.approx(max(min(Q * 0.004 / 20, 0.006 / 4), 0.5 * HOLE["size_m"]), rel=1e-5)
    assert h["size_m"] >= 0.5 * HOLE["size_m"] * (1 - 1e-6)
    assert applied[0]["done"] and applied[0]["rule"] == "AI-fea-1"


def test_coarser_hot_spot_keeps_only_its_shape_and_its_wall():
    zs, _ = A.apply_zones(ZONES, _decision(zones={"H1": {"priority": "coarser", "reason": "modal"}}), RECORD, diag_m=DIAG)
    h = _by_id(zs)["H1"]
    assert h["kind"] == "round" and h["was_kind"] == "hole" and h["rule"] == "PLAN-fea-7"
    assert h["size_m"] == pytest.approx(min(Q * 0.004 / 4, 0.006 / 2), rel=1e-5)
    assert "wall_elements" not in h


def test_coarser_on_a_wall_or_a_round_changes_nothing_and_says_so():
    zs, applied = A.apply_zones(ZONES, _decision(zones={"W3": {"priority": "coarser", "reason": "x"},
                                                        "R4": {"priority": "coarser", "reason": "x"}}),
                                RECORD, diag_m=DIAG)
    assert _by_id(zs)["W3"]["size_m"] == WALL["size_m"] and _by_id(zs)["R4"]["size_m"] == pytest.approx(ROUND["size_m"])
    assert all(not a["done"] for a in applied)


def test_a_sharp_corner_is_never_refined_and_coarser_caps_it_at_half_its_wall():
    zs, applied = A.apply_zones(ZONES, _decision(zones={"C5": {"priority": "refine", "reason": "x"}}), RECORD, diag_m=DIAG)
    assert _by_id(zs)["C5"]["size_m"] == CORNER["size_m"] and not applied[0]["done"]
    zs, _ = A.apply_zones(ZONES, _decision(zones={"C5": {"priority": "coarser", "reason": "x"}}), RECORD, diag_m=DIAG)
    assert _by_id(zs)["C5"]["size_m"] == pytest.approx(0.003)


def test_set_aside_only_a_small_hot_spot_never_a_wall():
    d = _decision(features={"H1": {"action": "set_aside", "reason": "x"}, "H2": {"action": "set_aside", "reason": "x"},
                            "W3": {"action": "set_aside", "reason": "x"}})
    zs, applied = A.apply_zones(ZONES, d, RECORD, diag_m=DIAG)
    z = _by_id(zs)
    assert z["H1"]["kind"] == "hole"                         # 4 mm is over 2% of 150 mm
    assert z["H2"]["kind"] == "round" and z["H2"]["ai"] == "set_aside"
    assert z["W3"]["size_m"] == WALL["size_m"]
    assert {a["id"] for a in applied if not a["done"]} == {"H1", "W3"}


def test_keep_a_micro_detail_only_when_a_mesh_can_follow_it():
    d = _decision(features={"M1": {"action": "keep", "reason": "x"}, "M2": {"action": "keep", "reason": "x"}})
    zs, applied = A.apply_zones(ZONES, d, RECORD, diag_m=DIAG)
    kept = [z for z in zs if z["kind"] == "detail"]
    assert [z["id"] for z in kept] == ["K1"] and kept[0]["size_m"] == pytest.approx(0.0005)
    assert [a["id"] for a in applied if not a["done"]] == ["M2"]


def test_no_choice_ever_goes_past_the_rules_minimums():
    """AI-fea-5 over every combination of choices: 2 through every wall, 4 per quarter-turn of every
    round or hot spot, the corner at half its wall at most."""
    ids = [z["id"] for z in ZONES]
    for combo in itertools.product(A.PRIORITIES, repeat=len(ids)):
        for aside in (False, True):
            d = _decision(zones={i: {"priority": p, "reason": ""} for i, p in zip(ids, combo, strict=True)},
                          features={i: {"action": "set_aside", "reason": ""} for i in ids} if aside else {})
            zs, _ = A.apply_zones(ZONES, d, RECORD, diag_m=DIAG)
            for z in zs:
                if z.get("thickness_m") and z["kind"] != "corner":
                    assert z["thickness_m"] / z["size_m"] >= P.WALL_ELEMENTS - 1e-6, (combo, z)
                if z["kind"] == "corner":
                    assert z["size_m"] <= z["thickness_m"] / 2 * (1 + 1e-6)
                if z.get("radius_m"):
                    assert Q * z["radius_m"] / z["size_m"] >= P.ROUND_PER_QUARTER - 1e-6, (combo, z)


def test_no_decision_is_the_rules_own_zones():
    zs, applied = A.apply_zones(ZONES, None, RECORD, diag_m=DIAG)
    assert zs == ZONES and applied == []


# ------------------------------------------------------------------ AI-fea-4: the budget ----------------
def test_the_users_budget_lowers_the_limit_never_raises_it():
    assert A.budget_limit(5_000_000, _decision(budget_elements=300_000)) == (300_000, "the user's own budget of 300,000 elements")
    assert A.budget_limit(5_000_000, _decision(budget_elements=20_000_000)) == (5_000_000, "")
    assert A.budget_limit(5_000_000, None) == (5_000_000, "")


# ------------------------------------------------------------------ the answer, checked -------------------
def _brief():
    return A.brief(RECORD, {"request_txt": "static stress, care about the bolt holes"}, ZONES, diag_m=DIAG, limit=5_000_000)


def test_the_answer_is_checked_and_choices_on_unseen_ids_are_dropped():
    raw = {"analysis": "modal", "matters": "stiffness", "zones": [{"id": "H1", "priority": "coarser", "reason": "r"},
                                                                  {"id": "H99", "priority": "refine", "reason": "r"},
                                                                  {"id": "W3", "priority": "max", "reason": "r"}],
           "features": [{"id": "M1", "action": "keep", "reason": "r"}], "joints": [], "budget_elements": 0,
           "words": ["a", "b"]}
    d, dropped = A.validate(json.dumps(raw), _brief())
    assert d["analysis"] == "modal" and set(d["zones"]) == {"H1"} and set(d["features"]) == {"M1"}
    assert len(dropped) == 2


@pytest.mark.parametrize("raw", ["not json", "[1, 2]", json.dumps({"analysis": "fatigue"})])
def test_an_answer_that_is_not_a_decision_is_refused(raw):
    d, dropped = A.validate(raw, _brief())
    assert d is None and dropped


def test_a_budget_nobody_means_is_ignored():
    raw = {"analysis": "static", "matters": "", "zones": [], "features": [], "joints": [], "budget_elements": 50, "words": []}
    d, dropped = A.validate(json.dumps(raw), _brief())
    assert d["budget_elements"] == 0 and dropped


def test_the_same_part_and_words_make_the_same_message():
    a = A.messages_for(_brief())
    b = A.messages_for(A.brief(RECORD, {"request_txt": "static stress, care about the bolt holes"},
                               list(reversed(ZONES)), diag_m=DIAG, limit=5_000_000))
    assert a == b


# ------------------------------------------------------------------ AI-fea-6: asking, and falling back -----
class _Router:
    def __init__(self, answer=None, exc=None):
        self.answer, self.exc, self.calls = answer, exc, 0

    async def call_builder_model(self, messages, tools=None, tool_choice="auto", job_id="", user_id="",
                                 parallel_tool_calls=None, on_reasoning=None):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.answer


def _answer(args: dict | str, name: str = A.TOOL_DECISION) -> ModelRoundResult:
    return ModelRoundResult(tool_calls=(ToolCallRequest(id="call_abcdef123", name=name,
                                                         arguments=args if isinstance(args, str) else json.dumps(args)),))


GOOD = {"analysis": "static", "matters": "the bolt hole", "zones": [{"id": "H1", "priority": "refine", "reason": "named"}],
        "features": [], "joints": [], "budget_elements": 0, "words": ["The bolt hole is refined."]}


@pytest.fixture
def router(monkeypatch):
    def install(r):
        monkeypatch.setattr(llm, "_router", r)
        return r
    return install


def _decide(**kw):
    return asyncio.run(A.decide(RECORD, {"request_txt": "care about the bolt holes", "user_id": "u1"}, ZONES,
                                diag_m=DIAG, limit=5_000_000, job_id="j1", enabled=True, **kw))


def test_a_good_answer_is_used(router):
    r = router(_Router(_answer(GOOD)))
    ai = _decide()
    assert ai["status"] == "used" and ai["decision"]["zones"] == {"H1": {"priority": "refine", "reason": "named"}}
    assert r.calls == 1


@pytest.mark.parametrize("make,status", [
    (lambda: _Router(exc=RuntimeError("provider down")), "failed"),
    (lambda: _Router(exc=TimeoutError()), "failed"),
    (lambda: _Router(ModelRoundResult(failure_marker="api_failure")), "failed"),
    (lambda: _Router(ModelRoundResult(assistant_text="I think refine the hole")), "invalid"),
    (lambda: _Router(_answer("{broken")), "invalid"),
])
def test_no_usable_answer_is_the_rules_own_plan_with_a_note(router, make, status):
    router(make())
    ai = _decide()
    assert ai["status"] == status and ai["decision"] == {}
    assert "rules' own mesh" in ai["note"]
    assert A.words(ai) == [ai["note"]]
    assert A.decisions(ai)[0]["rule"] == "AI-fea-6"


def test_no_router_at_all_is_the_rules_own_plan(router):
    router(None)
    assert _decide()["status"] == "failed"


def test_fea_ai_off_asks_nobody(router):
    r = router(_Router(_answer(GOOD)))
    ai = asyncio.run(A.decide(RECORD, {}, ZONES, diag_m=DIAG, limit=5_000_000, enabled=False))
    assert ai["status"] == "off" and r.calls == 0 and A.words(ai) == []


def test_the_same_part_replays_the_same_decision_from_the_cache(router):
    """Run twice: the second decision is the first one's record - the model is asked once."""
    from meshpipeline.contracts.decision_cache import CachingModelRouter, MemoryDecisions

    inner = _Router(_answer(GOOD))
    router(CachingModelRouter(inner, model_of=lambda _s: "gpt-test", store=MemoryDecisions(), mode="reuse"))
    first, second = _decide(), _decide()
    assert inner.calls == 1
    assert first["decision"] == second["decision"] and first["key"] == second["key"]


# ------------------------------------------------------------------ AI-fea-8: levers ---------------------
FAILED_FILL = {"pass": 1, "rc": 3, "built": False, "valid": False, "min_sicn": None, "cells": 0, "limit": 100,
               "over": False, "short": [], "bad_surfaces": [4, 9], "out_of_memory": False}


def test_the_ladders_own_lever_leads_the_options():
    opts = L.lever_options(FAILED_FILL, [], {})
    assert [o["lever"] for o in opts] == ["LAD-fea-2", "LAD-fea-1", "LAD-fea-6"]
    assert L.next_lever(FAILED_FILL, [], {}) == opts[0]
    pulled = [opts[0]]
    assert [o["lever"] for o in L.lever_options(FAILED_FILL, pulled, {})] == []     # its surfaces were re-meshed already: the ladder stops


def test_a_lever_is_only_ever_one_of_the_options(router):
    opts = L.lever_options(FAILED_FILL, [], {})
    used = {"status": "used", "decision": {"analysis": "static"}}
    router(_Router(_answer({"lever": "LAD-fea-1", "reason": "gentler"}, A.TOOL_LEVER)))
    lever, picked = asyncio.run(A.choose_lever(opts, FAILED_FILL, ai=used, state={}))
    assert lever["lever"] == "LAD-fea-1" and picked["status"] == "used"
    router(_Router(_answer({"lever": "LAD-fea-4", "reason": "x"}, A.TOOL_LEVER)))
    lever, picked = asyncio.run(A.choose_lever(opts, FAILED_FILL, ai=used, state={}))
    assert lever["lever"] == "LAD-fea-2" and picked["status"] == "invalid"
    lever, picked = asyncio.run(A.choose_lever(opts, FAILED_FILL, ai={"status": "off"}, state={}))
    assert lever["lever"] == "LAD-fea-2" and picked == {}


# ------------------------------------------------------------------ the plan, on a real part --------------
def test_the_plan_carries_the_decision_its_words_and_its_why(tmp_path):
    pytest.importorskip("OCP.BRepPrimAPI")
    pytest.importorskip("gmsh")
    from tests import fea_fixtures as fx
    from tests.unit.engines.test_fea_driver import _stage

    from meshpipeline.engines import fea_driver as FD

    ws = _stage(fx.plate_with_hole(), tmp_path / "plate")
    record, surface, diag = FD.measure_staged(ws)
    rules = FD.choose_plan(record, surface, diag)
    spot = next(z for z in rules["zones"] if z["kind"] in ("hole", "fillet", "notch"))
    ai = A.section(_decision(zones={spot["id"]: {"priority": "refine", "reason": "named"}}, budget_elements=200_000),
                   [], status="used", key="k")
    plan = FD.choose_plan(record, surface, diag, ai=ai)
    assert _by_id(plan["zones"])[spot["id"]]["size_m"] < spot["size_m"]
    assert plan["limit"]["elements"] == 200_000 and "budget" in plan["limit"]["why"]
    assert plan["ai"]["applied"][0]["done"] and any("bolt hole" in w for w in plan["words"])
    assert any(d["rule"] == "AI-fea-1" and d.get("source") == "ai-lever" for d in plan["decisions"])


# ------------------------------------------------------------------ review follow-ups ---------------------
@pytest.mark.parametrize("raw", [{"analysis": "static", "zones": 5}, {"analysis": "static", "features": "x"},
                                 {"analysis": "static", "joints": [1]}, {"analysis": "static", "words": "one"}])
def test_an_answer_of_the_wrong_shape_is_refused_never_raised(raw):
    d, dropped = A.validate(json.dumps(raw), _brief())
    assert d is None and "schema" in dropped[0]


def test_with_no_time_to_spare_the_model_is_not_asked(router):
    r = router(_Router(_answer(GOOD)))
    ai = _decide(wait_s=5)
    assert ai["status"] == "no_time" and r.calls == 0 and "rules' own mesh" in ai["note"]


def test_a_refined_zone_carries_the_targets_the_checks_hold_it_to():
    zs, _ = A.apply_zones(ZONES, _decision(zones={"H1": {"priority": "refine", "reason": ""},
                                                  "W3": {"priority": "refine", "reason": ""}}), RECORD, diag_m=DIAG)
    h, w = _by_id(zs)["H1"], _by_id(zs)["W3"]
    assert h["per_quarter"] == pytest.approx(min(20, Q * h["radius_m"] / h["size_m"]), rel=1e-2)
    assert h["wall_elements"] == pytest.approx(min(4, h["thickness_m"] / h["size_m"]), rel=1e-2)
    assert w["wall_elements"] == pytest.approx(3, rel=1e-2)


def test_the_words_name_the_limit_the_plan_holds():
    ai = A.section(_decision(budget_elements=2_000_000, zones={"H1": {"priority": "refine", "reason": ""}}),
                   [{"id": "H1", "choice": "refine", "done": True}], status="used")
    said = A.words(ai, {"limit": {"elements": 1_000_000}, "coarsened": {"zone_floor_m": 0.001}})
    assert any("own limit, 1,000,000, is lower" in w for w in said)
    assert any("coarser, as it does for any plan" in w for w in said)
    said = A.words(ai, {"limit": {"elements": 2_000_000}})
    assert any("2,000,000, is the limit" in w for w in said)


def test_a_zone_the_ai_did_not_touch_is_exactly_the_rules_own():
    """A rules size rounded a hair over its floor (t/2 to 6 digits) is never nudged: the plan of a
    part the AI changed nothing on is the rules' plan, so its mesh is the rules' mesh."""
    wall = {**WALL, "size_m": float(f"{0.0061234567 / 2:.6g}"), "thickness_m": 0.0061234567}
    zs, _ = A.apply_zones([wall, HOLE], _decision(zones={"H1": {"priority": "refine", "reason": ""}}), RECORD, diag_m=DIAG)
    assert _by_id(zs)["W3"] == wall


def test_refinements_that_would_not_fit_are_left_out_before_any_rules_zone_is_coarsened(tmp_path, monkeypatch):
    """AI-fea-5 (lab, 24 Oct): a refinement that takes the plan over its limit is dropped first, so
    the rules' own zones are never coarsened for an extra the AI asked for."""
    pytest.importorskip("OCP.BRepPrimAPI")
    pytest.importorskip("gmsh")
    from tests import fea_fixtures as fx
    from tests.unit.engines.test_fea_driver import _stage

    from meshpipeline.engines import fea_driver as FD

    ws = _stage(fx.plate_with_hole(), tmp_path / "plate")
    record, surface, diag = FD.measure_staged(ws)
    rules = FD.choose_plan(record, surface, diag)
    every = {z["id"]: {"priority": "refine", "reason": ""} for z in rules["zones"]}
    ai = A.section(_decision(zones=every), [], status="used", key="k")
    big = FD.choose_plan(record, surface, diag, ai=ai)
    assert big["estimate"]["elements"] > rules["estimate"]["elements"]
    # a limit the rules' plan fits under and the refined one does not
    cap = int(rules["estimate"]["elements"] / P.LIMIT_AIM) + 1
    monkeypatch.setattr(FD, "element_limit", lambda _record, _ceiling=None: (cap, "a test limit"))
    plan = FD.choose_plan(record, surface, diag, ai=ai)
    assert plan["fits"] and not plan.get("coarsened")
    assert [(z["id"], z["size_m"]) for z in plan["zones"]] == [(z["id"], z["size_m"]) for z in rules["zones"]]
    assert plan["ai"]["refines_dropped"] == sorted(every)
    assert any("left out" in w for w in plan["words"])


def test_an_assemblys_joints_are_suggested_for_the_user_to_confirm_never_applied():
    """AI-fea-7 on #281's pair facts: the AI sees where the parts meet and the user's own choice; a
    suggestion on a shown pair is kept and said, one on any other pair is dropped, and no zone moves."""
    rec = {**RECORD, "assembly_plan": {"connection": "bonded", "parts": [{"id": "Part_1", "name": "Flange"},
                                                                         {"id": "Part_2", "name": "Bolt M12"}],
                                       "pair_list": [{"pair": "Interface_1", "kind": "touching",
                                                      "parts": ["Part_1", "Part_2"], "area_mm2": 113.1}]}}
    b = A.brief(rec, {"request_txt": "bolted flange, static"}, ZONES, diag_m=DIAG, limit=5_000_000)
    assert b["assembly"]["touching_pairs"][0]["pair"] == "Interface_1"
    assert b["assembly"]["connection_user_chose"] == "bonded"
    raw = {"analysis": "static", "matters": "the bolt", "zones": [], "features": [], "budget_elements": 0, "words": [],
           "joints": [{"pair": "Interface_1", "suggest": "contact", "reason": "a bolted joint can open"},
                      {"pair": "Interface_9", "suggest": "bonded", "reason": "x"}]}
    d, _ = A.validate(json.dumps(raw), b)
    assert d["joints"] == [{"pair": "Interface_1", "suggest": "contact", "reason": "a bolted joint can open"}]
    ai = A.section(d, [], status="used")
    assert any("Interface_1: contact" in w and "Please confirm" in w for w in A.words(ai))
    assert any(e["rule"] == "AI-fea-7" and e["value"] == "contact" for e in A.decisions(ai))
    zs, applied = A.apply_zones(ZONES, d, rec, diag_m=DIAG)
    assert zs == ZONES and applied == []
