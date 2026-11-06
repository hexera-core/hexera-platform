# Responsibility: Verify PLAN-fea-27/28 and AI-fea-4: an element count the user named is read by the AI from their notes and checked (a target, a range, a ceiling or a floor; garbage refused; the AI down said), and the whole FEA plan - base and every zone - is scaled by one factor to it, both ways, the hard minimums re-applied, never past the largest machine, said in plain words.
# Boundaries: the solver runs against a made-up count model (no geometry); one plate with a hole is planned for real (gmsh + OCP, skipped without them). Nothing is meshed.
from __future__ import annotations

import json
import math

import pytest

from meshpipeline.engines import fea_ai as A
from meshpipeline.engines import fea_driver as FD
from meshpipeline.engines import fea_plan as P

Q = math.pi / 2.0


# ------------------------------------------------------------------ the zones, scaled ---------------------
WALL = {"id": "W3", "kind": "wall", "faces": ["Face_3"], "size_m": 0.002, "reach_m": 0.004, "thickness_m": 0.004}
HOLE = {"id": "H1", "kind": "hole", "faces": ["Face_7"], "size_m": Q * 0.006 / 6, "reach_m": 0.003,
        "radius_m": 0.006, "spot": "H1"}
ROUND = {"id": "R9", "kind": "round", "faces": ["Face_9"], "size_m": Q * 0.01 / 4, "reach_m": 0.0, "radius_m": 0.01}
CORNER = {"id": "C2", "kind": "corner", "faces": ["Face_1", "Face_2"], "size_m": 0.001, "reach_m": 0.001,
          "thickness_m": 0.004, "capped": True}
PART = {"id": "P1", "kind": "part", "faces": ["Part_1_Face_1"], "size_m": 0.01, "reach_m": 0.0}


def _by(zs):
    return {z["id"]: z for z in zs}


def test_coarser_every_zone_scales_until_its_hard_minimum_holds_it():
    zs = _by(P.scaled([WALL, HOLE, ROUND, CORNER, PART], 1.8))
    # PLAN-fea-3: 2 through the wall - the wall's own rule size is its minimum already
    assert zs["W3"]["size_m"] == pytest.approx(0.002) and zs["W3"]["held"] == "PLAN-fea-3"
    # PLAN-fea-7: the hole keeps 4 per quarter-turn (6 -> 4 is x1.5, so x1.8 is held there), named less accurate
    assert Q * HOLE["radius_m"] / zs["H1"]["size_m"] == pytest.approx(4.0, rel=1e-3)
    assert zs["H1"]["held"] == "PLAN-fea-7" and zs["H1"]["below_accuracy"]
    assert zs["H1"]["per_quarter"] == pytest.approx(4.0, rel=1e-3)          # what the mesh check holds it to
    assert zs["R9"]["size_m"] == pytest.approx(ROUND["size_m"], rel=1e-6)   # at its minimum already
    # a corner grows from a quarter of its wall to (at most) half of it; a part's base to a fifth of the part
    assert zs["C2"]["size_m"] == pytest.approx(0.0018) and "held" not in zs["C2"]
    assert zs["P1"]["size_m"] == pytest.approx(0.018)
    assert _by(P.scaled([CORNER, PART], 3.0))["C2"]["size_m"] == pytest.approx(0.002)
    assert _by(P.scaled([CORNER, PART], 3.0))["P1"]["held"] == "PLAN-fea-28"
    for z in zs.values():
        assert z["unscaled_m"] == pytest.approx(_by([WALL, HOLE, ROUND, CORNER, PART])[z["id"]]["size_m"])


def test_finer_every_zone_scales_by_the_one_factor_but_a_capped_corner_keeps_its_cap():
    zs = _by(P.scaled([WALL, HOLE, ROUND, CORNER], 0.5))
    for z in (WALL, HOLE, ROUND):
        assert zs[z["id"]]["size_m"] == pytest.approx(0.5 * z["size_m"], rel=1e-5)
        assert "per_quarter" not in zs[z["id"]] and not zs[z["id"]].get("below_accuracy")
    assert zs["C2"]["size_m"] == pytest.approx(CORNER["size_m"]) and zs["C2"]["held"] == "PLAN-fea-5"


def test_the_base_grows_to_a_fifth_of_the_part_at_most():
    assert P.base_size(1.0) == pytest.approx(0.1)
    assert P.base_size(1.0, 0.5) == pytest.approx(0.05)
    assert P.base_size(1.0, 1.5) == pytest.approx(0.15)
    assert P.base_size(1.0, 3.0) == pytest.approx(1.0 / P.TARGET_MIN_DIVISIONS)


# ------------------------------------------------------------------ the solve, on a count model -----------
def _model(monkeypatch, *, fine: float, floor: float = 0.0, limit: int = 50_000_000):
    """make_plan whose estimate is floor + fine / s^3 (`floor`: elements the minimums hold whatever s)."""
    calls = []

    def fake(record, surface, diag_m, *, levers=None, limit=0, ai=None, scale=None, **_k):
        s = float(scale or 1.0)
        calls.append(s)
        n = floor + fine / s ** 3
        zones = [dict(WALL, held="PLAN-fea-3")] if (floor and s > 1) else []
        return {"estimate": {"elements": int(n)}, "fits": n <= P.LIMIT_AIM * limit, "zones": zones,
                "base_size_m": P.base_size(diag_m, s), "limit": {"elements": limit}}

    monkeypatch.setattr(FD, "make_plan", fake)
    return calls, limit


def _solve(target, *, limit):
    rules = FD.make_plan({}, None, 1.0, limit=limit)
    return FD.target_plan({}, None, 1.0, levers=[], ai=None, target=target, limit=limit, rules=rules)


@pytest.mark.parametrize("asked", [300_000, 3_000_000])
def test_the_one_factor_is_solved_on_the_estimate_both_ways(monkeypatch, asked):
    calls, limit = _model(monkeypatch, fine=1_000_000)
    plan = _solve({"elements": asked, "mode": "about"}, limit=limit)
    t = plan["target"]
    assert abs(plan["estimate"]["elements"] / asked - 1.0) <= P.TARGET_TOLERANCE
    assert (t["scale"] > 1.0) == (asked < 1_000_000)
    assert t["steps"] <= P.TARGET_STEPS and not t["held_by_minimums"]


def test_where_the_minimums_hold_the_rest_takes_up_the_slack_and_the_stop_is_said(monkeypatch):
    # 600k held by the minimums whatever s, 400k that scales: 700k is reachable (more than x1/s^3 alone gives) ...
    _model(monkeypatch, fine=400_000, floor=600_000)
    plan = _solve({"elements": 700_000, "mode": "about"}, limit=50_000_000)
    assert abs(plan["estimate"]["elements"] / 700_000 - 1.0) <= P.TARGET_TOLERANCE
    assert plan["target"]["scale"] > (1_000_000 / 700_000) ** (1 / 3)        # went further than the plain guess
    # ... 300k is not: the plan stops near the minimums' floor and says so
    _model(monkeypatch, fine=400_000, floor=600_000)
    low = _solve({"elements": 300_000, "mode": "about"}, limit=50_000_000)
    assert low["target"]["held_by_minimums"] and low["estimate"]["elements"] < 750_000
    words = " ".join(P.target_words({**low, "plan": None}))
    assert "The minimums stop it" in words and "2 elements through the 4 mm wall under Face_3" in words


def test_a_count_past_the_largest_machine_is_aimed_at_the_most_it_holds(monkeypatch):
    _calls, limit = _model(monkeypatch, fine=1_000_000, limit=2_000_000)
    plan = _solve({"elements": 50_000_000, "mode": "about"}, limit=limit)
    assert plan["fits"] and plan["target"]["machine"]
    assert plan["estimate"]["elements"] <= P.LIMIT_AIM * limit
    assert plan["target"]["aim"] < 50_000_000


def test_a_ceiling_or_floor_the_rules_already_meet_keeps_the_rules_plan(monkeypatch):
    calls, limit = _model(monkeypatch, fine=1_000_000)
    plan = _solve({"elements": 2_000_000, "mode": "at_most"}, limit=limit)
    assert plan["target"]["kept_rules"] and plan["target"]["scale"] == 1.0 and calls == [1.0]
    plan = _solve({"elements": 500_000, "mode": "at_least"}, limit=limit)
    assert plan["target"]["kept_rules"]
    # a ceiling the rules' plan is over is scaled to, as "about" is
    plan = _solve({"elements": 500_000, "mode": "at_most"}, limit=limit)
    assert plan["target"]["scale"] > 1.0 and abs(plan["estimate"]["elements"] / 500_000 - 1) <= P.TARGET_TOLERANCE


# ------------------------------------------------------------------ a real plan --------------------------
def test_a_real_part_is_scaled_to_the_count_both_ways_with_its_words_and_why(tmp_path):
    pytest.importorskip("OCP.BRepPrimAPI")
    pytest.importorskip("gmsh")
    from tests import fea_fixtures as fx
    from tests.unit.engines.test_fea_driver import _stage

    ws = _stage(fx.plate_with_hole(), tmp_path / "plate")
    record, surface, diag = FD.measure_staged(ws)
    rules = FD.choose_plan(record, surface, diag)
    n0 = rules["estimate"]["elements"]
    finer = FD.choose_plan(record, surface, diag, target={"elements": 4 * n0, "mode": "about", "source": "notes",
                                                          "quote": "x elements"})
    assert abs(finer["estimate"]["elements"] / (4 * n0) - 1.0) <= P.TARGET_TOLERANCE
    s = finer["target"]["scale"]
    assert s < 1.0 and finer["base_size_m"] == pytest.approx(rules["base_size_m"] * s, rel=1e-3)
    by_rules = {z["id"]: z for z in rules["zones"]}
    for z in finer["zones"]:
        if z["kind"] != "corner":
            assert z["size_m"] == pytest.approx(by_rules[z["id"]]["size_m"] * s, rel=1e-3)
    assert any(w.startswith("You asked for about") and f"x{s:.3g} (finer)" in w for w in finer["words"])
    assert any(d["rule"] == "PLAN-fea-27" for d in finer["decisions"])
    assert finer["limit"]["elements"] == rules["limit"]["elements"]
    # coarser: the plate's walls hold it (2 through the 4 mm plate) and that is said
    coarse = FD.choose_plan(record, surface, diag, target={"elements": n0 // 5, "mode": "about", "source": "notes",
                                                           "quote": "x elements"})
    for z in coarse["zones"]:
        if z["kind"] in ("wall", "wall_spot"):
            assert z["size_m"] <= z["thickness_m"] / P.WALL_ELEMENTS * (1 + 1e-6)
        if z["kind"] in ("hole", "fillet", "notch", "round"):
            assert Q * z["radius_m"] / z["size_m"] >= P.ROUND_PER_QUARTER * (1 - 1e-3)
    assert coarse["target"]["held_by_minimums"]
    assert any("The minimums stop it" in w for w in coarse["words"])
    assert P.target_remark(coarse).startswith("you asked for about")


def test_a_range_is_aimed_at_its_middle_and_any_estimate_inside_it_is_a_hit(monkeypatch):
    calls, limit = _model(monkeypatch, fine=1_000_000)
    plan = _solve({"elements": 450_000, "mode": "range", "low": 300_000, "high": 600_000}, limit=limit)
    t = plan["target"]
    assert 300_000 <= plan["estimate"]["elements"] <= 600_000 and t["hit"]
    assert len(calls) == 2                                      # the first step landed inside: no more estimates
    assert any("between 300k and 600k elements" in w and "inside your range" in w for w in P.target_words(plan))
    # the rules' own plan inside the range is kept as it is
    calls, limit = _model(monkeypatch, fine=1_000_000)
    plan = _solve({"elements": 1_100_000, "mode": "range", "low": 700_000, "high": 1_500_000}, limit=limit)
    assert plan["target"]["kept_rules"] and calls == [1.0]


# ------------------------------------------------------------------ AI-fea-4: the AI reads the count -------
BASE_ANSWER = {"analysis": "static", "matters": "the bolt hole", "zones": [], "features": [], "joints": [],
               "words": ["The bolt hole matters."]}


def _answer(**kw) -> dict | None:
    d, _dropped = A.validate(json.dumps({**BASE_ANSWER, **kw}), {"zones": [], "micro_details": []})
    return d


@pytest.mark.parametrize(("fields", "want"), [
    # one count
    ({"target_elements": 1_000_000}, (1_000_000, 0, 0, "about")),
    ({"target_elements": 1_000_000.0, "target_min": 0, "target_max": 0}, (1_000_000, 0, 0, "about")),
    # a range: aimed at its middle (given or not)
    ({"target_elements": 1_100_000, "target_min": 700_000, "target_max": 1_500_000},
     (1_100_000, 700_000, 1_500_000, "range")),
    ({"target_min": 700_000, "target_max": 1_500_000}, (1_100_000, 700_000, 1_500_000, "range")),
    # a target outside its own range: the middle
    ({"target_elements": 9_000_000, "target_min": 700_000, "target_max": 1_500_000},
     (1_100_000, 700_000, 1_500_000, "range")),
    # a ceiling or a floor alone
    ({"target_max": 300_000}, (300_000, 0, 300_000, "at_most")),
    ({"target_min": 2_000_000}, (2_000_000, 2_000_000, 0, "at_least")),
    # none
    ({"target_elements": 0, "target_min": 0, "target_max": 0}, (0, 0, 0, "")),
    ({}, (0, 0, 0, "")),
    # an answer saved before 5 Nov named the count budget_elements
    ({"budget_elements": 500_000}, (500_000, 0, 0, "about")),
])
def test_the_ais_count_is_checked_into_a_target(fields, want):
    d = _answer(**fields)
    assert (d["target_elements"], d["target_min"], d["target_max"], d["target_mode"]) == want


@pytest.mark.parametrize("fields", [
    {"target_elements": -1_000_000},                       # negative
    {"target_elements": 50},                               # no mesh count anyone means
    {"target_elements": 10 ** 13},                         # no machine's
    {"target_elements": "a million"},                      # not a number
    {"target_elements": True},
    {"target_elements": float("nan")},
    {"target_min": 2_000_000, "target_max": 1_000_000},    # min over max
    {"target_min": -5, "target_max": -1},
])
def test_a_garbage_count_is_refused_with_a_note_and_the_rest_of_the_answer_stands(fields):
    d, dropped = A.validate(json.dumps({**BASE_ANSWER, **fields}, allow_nan=True), {"zones": [], "micro_details": []})
    assert d is not None and d["target_elements"] == 0 and d["target_mode"] == "" and dropped
    assert d["matters"] == "the bolt hole"


def test_the_prompt_asks_for_a_mesh_count_only():
    tool = A.decision_tool()["function"]["parameters"]
    assert {"target_elements", "target_min", "target_max"} <= set(tool["required"])
    assert "budget_elements" not in tool["properties"]
    from meshpipeline.engines.fea_ai import SYSTEM
    for words in ("element COUNT", "M8", "500 MPa", "1000 N", "NODES"):
        assert words in SYSTEM


def test_the_driver_takes_the_count_the_ai_read():
    used = {"status": "used", "decision": _answer(target_elements=1_000_000)}
    assert FD.element_target({}, used) == {"elements": 1_000_000, "mode": "about", "quote": "", "source": "ai"}
    used = {"status": "used", "decision": _answer(target_min=700_000, target_max=1_500_000)}
    assert FD.element_target({}, used) == {"elements": 1_100_000, "mode": "range", "quote": "", "source": "ai",
                                           "low": 700_000, "high": 1_500_000}
    assert FD.element_target({}, {"status": "used", "decision": _answer()}) is None


def test_with_the_ai_down_a_count_in_the_notes_is_not_applied_and_that_is_said(monkeypatch):
    notes = [{"id": "n1", "text": "about 1M elements please", "source": "console", "turn": None, "at": ""}]
    down = {"status": "error", "note": "the AI did not answer", "decision": {}}
    target = FD.element_target({"user_notes": notes}, down)
    assert target == {"unread": True}
    assert FD.element_target({}, down) is None                    # no notes: nothing to say
    calls, limit = _model(monkeypatch, fine=1_000_000)
    monkeypatch.setattr(FD, "element_limit", lambda _r, _c=None: (limit, "a test limit"))
    monkeypatch.setattr(FD, "_restate", lambda record, plan: {**plan, "words": P.target_words(plan)})
    plan = FD.choose_plan({}, None, 1.0, target=target)
    assert calls == [1.0] and not plan.get("target")              # the rules' own plan
    assert plan["words"] == [P.UNREAD_WORDS]
    assert "not applied this run" in P.target_remark(plan)
