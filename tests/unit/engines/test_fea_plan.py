# Responsibility: Verify the FEA plan's rules as pure arithmetic over a measurement record: the zones each rule asks for, the base size solved for the detail level, the budget check and its options, the ladder's levers and the delivered pass, and that the same inputs give the same plan.
# Boundaries: no geometry, no engine: records and histograms are written by hand.
from __future__ import annotations

import json
import math

import pytest

from meshpipeline.engines import fea_ladder as L
from meshpipeline.engines import fea_plan as P
from meshpipeline.engines.fea_size_field import Histogram, elements, group_zones

#: A 200 x 100 x 5 mm plate with a 10 mm hole and one sharp inside corner, as cad/fea_measure says it.
RECORD = {
    "bodies": 1,
    "thickness": {"min_mm": 5.0, "face": "Face_3"},
    "faces": {
        "Face_1": {"kind": "plane", "thickness_min_mm": 200.0},
        "Face_3": {"kind": "plane", "thickness_min_mm": 5.0},
        "Face_7": {"kind": "cylinder", "thickness_min_mm": 40.0, "radius_mm": 10.0, "concave": True},
        "Face_8": {"kind": "cylinder", "radius_mm": 30.0, "concave": False},
    },
    "hot_spots": [
        {"id": "H1", "type": "sharp_corner", "faces": ["Face_1", "Face_3"], "angle_deg": 90.0,
         "cap_basis_mm": 8.0, "local_thickness_mm": 8.0, "capped": True},
        {"id": "H2", "type": "hole", "shape": "hole", "faces": ["Face_7"], "radius_mm": 10.0,
         "local_thickness_mm": 40.0},
    ],
}


def _zone(zs, zid):
    return next(z for z in zs if z["id"] == zid)


def test_each_rule_asks_for_its_own_size():
    zs = P.zones(RECORD)
    wall = _zone(zs, "W3")
    assert wall["rule"] == "PLAN-fea-3" and wall["size_m"] == pytest.approx(2.5e-3) and wall["reach_m"] == pytest.approx(5e-3)
    hole = _zone(zs, "H2")
    assert hole["rule"] == "PLAN-fea-4"
    # 10 per quarter-turn of a 10 mm radius (the 40 mm wall beside it asks for less)
    assert hole["size_m"] == pytest.approx(math.pi / 2 * 10e-3 / P.HOT_SPOT_PER_QUARTER, rel=1e-5)
    corner = _zone(zs, "C1")
    assert corner["rule"] == "PLAN-fea-5" and corner["capped"] and corner["size_m"] == pytest.approx(2e-3)
    # the hole face is a hot spot, so not a plain round; the convex round keeps its shape
    assert not any(z["id"] == "R7" for z in zs)
    assert _zone(zs, "R8")["rule"] == "PLAN-fea-7"


def test_a_hot_spot_in_a_thin_wall_gets_three_through_it():
    rec = json.loads(json.dumps(RECORD))
    rec["hot_spots"][1]["local_thickness_mm"] = 3.0
    assert _zone(P.zones(rec), "H2")["size_m"] == pytest.approx(1e-3)


def test_the_relaxed_option_asks_for_the_fewest_the_rule_allows():
    hole = _zone(P.zones(RECORD, relaxed=True), "H2")
    assert hole["size_m"] == pytest.approx(math.pi / 2 * 10e-3 / P.HOT_SPOT_PER_QUARTER_MIN, rel=1e-5)


def test_a_tightened_zone_is_finer_by_the_factor():
    assert _zone(P.zones(RECORD, tighten={"W3": 0.8}), "W3")["size_m"] == pytest.approx(2e-3)


def test_zone_groups_never_coarsen_a_zone():
    zs = P.zones(RECORD)
    for z in zs:
        g = next(g for g in group_zones(zs) if (z["kind"] == "corner") == g["edge"] and
                 set(z["faces"]) <= set(g["faces"]) and g["size"] <= z["size_m"] < g["size"] * 1.2001)
        assert g["reach"] >= z["reach_m"]


HIST = Histogram(rows=[(2e-3, 1e-6), (math.inf, 9e-5)])   # 100 cm3, 1 cm3 of it in a 2 mm zone


def test_the_base_size_is_solved_for_the_level():
    base = P.base_size(HIST, target=500_000, coarsest=0.02)
    assert elements(HIST, base, P.PER_H3) == pytest.approx(P.AIM * 500_000, rel=1e-6)
    # a part the level cannot fill sits at the coarsest base
    assert P.base_size(HIST, target=10, coarsest=0.02) == 0.02


def test_a_plan_that_fits_meshes_and_says_so_in_words():
    plan = P.finish(RECORD, P.zones(RECORD), HIST, level=P.LEVELS["standard"], diag_m=0.2236)
    assert plan["fits"] and plan["estimate"]["elements"] == pytest.approx(425_000, rel=1e-3)
    assert plan["words"][0].startswith("Detail level Standard")
    assert any("within the Standard level" in w for w in plan["words"])
    rules = {d["rule"] for d in plan["decisions"]}
    assert {"PLAN-fea-1", "PLAN-fea-2", "PLAN-fea-3", "PLAN-fea-4", "PLAN-fea-5", "PLAN-fea-6",
            "PLAN-fea-8"} <= rules


def test_over_the_level_the_plan_stops_with_its_options():
    heavy = Histogram(rows=[(1e-4, 1e-7), (math.inf, 9e-5)])     # 0.1 cm3 at 0.1 mm: ~450k on its own
    relaxed = Histogram(rows=[(1.7e-4, 1e-7), (math.inf, 9e-5)])
    plan = P.finish(RECORD, P.zones(RECORD), heavy, level=P.LEVELS["draft"], diag_m=0.2236,
                    relaxed_hist=relaxed)
    assert not plan["fits"]
    kinds = [o["option"] for o in plan["options"]]
    assert kinds == ["level_up", "fewer_at_spots"]
    assert plan["options"][0]["label"] == "Standard"
    assert any(w.startswith("Stopped before meshing") for w in plan["words"])
    budget = next(d for d in plan["decisions"] if d["rule"] == "PLAN-fea-8")
    assert budget["value"] == "over" and budget["alternatives"] == kinds


def test_the_same_inputs_give_the_same_plan():
    a = P.finish(RECORD, P.zones(RECORD), HIST, level=P.LEVELS["max"], diag_m=0.2236)
    b = P.finish(json.loads(json.dumps(RECORD)), P.zones(RECORD), Histogram.from_list(HIST.as_list()),
                 level=P.LEVELS["max"], diag_m=0.2236)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_one_part_only():
    assert P.one_part({"bodies": 1}) == ""
    assert "3 separate bodies" in P.one_part({"bodies": 3})


def test_the_levels_are_areens():
    assert {k: (v.label, v.target) for k, v in P.LEVELS.items()} == {
        "draft": ("Quick", 100_000), "standard": ("Standard", 500_000), "max": ("Fine", 2_000_000)}
    assert P.level_of("") is P.LEVELS["standard"]


# #
# the ladder
# #

PLAN = {"detail": {"ceiling": 125_000}, "estimate": {"elements": 85_000}}


def _q(**kw):
    q = {"cells": 90_000, "min_sicn": 0.4, "fatal": [], "fea": {"rules": {"short": []}}}
    q.update(kw)
    return q


def test_the_first_failure_pulls_its_lever_once():
    assert L.next_lever(L.pass_facts(1, 3, {}, PLAN), [], PLAN)["lever"] == "LAD-fea-2"
    bad = L.pass_facts(1, 0, _q(min_sicn=0.02), PLAN)
    lever = L.next_lever(bad, [], PLAN)
    assert lever["lever"] == "LAD-fea-1"
    assert L.next_lever(bad, [lever], PLAN) is None
    over = L.pass_facts(1, 0, _q(cells=200_000), PLAN)
    lv = L.next_lever(over, [], PLAN)
    assert lv["lever"] == "LAD-fea-4" and lv["factor"] < 1
    short = L.pass_facts(1, 0, _q(fea={"rules": {"short": ["W3"]}}), PLAN)
    lv = L.next_lever(short, [], PLAN)
    assert lv["lever"] == "LAD-fea-3" and lv["zones"] == ["W3"]
    assert L.next_lever(L.pass_facts(1, 0, _q(), PLAN), [], PLAN) is None


def test_the_ladder_stops_at_three_passes():
    bad = L.pass_facts(3, 0, _q(min_sicn=0.02), PLAN)
    assert L.next_lever(bad, [{"lever": "LAD-fea-2"}, {"lever": "LAD-fea-3", "zones": []}], PLAN) is None


def test_levers_change_only_their_own_settings():
    levers = [{"lever": "LAD-fea-1"}, {"lever": "LAD-fea-4", "factor": 0.9}, {"lever": "LAD-fea-3", "zones": ["W3"]}]
    assert L.growth(levers) == L.GENTLE_GROWTH and L.base_factor(levers) == 0.9
    assert L.tightened(levers) == {"W3": L.TIGHTEN}
    assert L.mesh_settings(levers) == {"algorithm3d": "delaunay", "optimise_passes": L.OPTIMISE_PASSES}
    assert L.mesh_settings([]) == {"algorithm3d": "delaunay", "optimise_passes": 1}


def test_the_best_pass_is_delivered_by_the_fixed_order():
    a = L.pass_facts(1, 0, _q(fea={"rules": {"short": ["W3"]}}), PLAN)
    b = L.pass_facts(2, 0, _q(min_sicn=0.3), PLAN)
    assert L.best_pass([a, b]) == (2, "every rule met")
    assert L.best_pass([L.pass_facts(1, 3, {}, PLAN)]) == (None, "no valid mesh")


def test_ai_levers_are_off():
    assert L.AI_LEVERS is False


def test_the_user_reads_the_stop_and_its_options_and_no_retry_is_spent():
    from meshpipeline.contracts.failure_cause import FailureCause, describe, retry_can_help

    facts = {"fea": True, "before_meshing": True, "deterministic": True, "cells": 550_539,
             "level": "Quick", "complete": True,
             "options": ["Use the Standard level: about 550,539 elements, every rule kept."]}
    what, then = describe(FailureCause.CELL_BUDGET, facts)
    assert what.startswith("Stopped before meshing") and "550,539 elements" in what and "Quick" in what
    assert "Standard level" in then
    assert not retry_can_help(FailureCause.CELL_BUDGET, facts)
    # a mesh that came out too big is still worded and retried as before
    assert retry_can_help(FailureCause.CELL_BUDGET, {"cells": 9_000_000, "limit": 8_000_000})
    assert describe(FailureCause.CELL_BUDGET, {"cells": 9_000_000, "limit": 8_000_000})[0].startswith(
        "The mesh came out too big")


def test_the_over_level_lever_makes_the_base_coarser():
    level = P.LEVELS["standard"]
    plain = P.finish(RECORD, P.zones(RECORD), HIST, level=level, diag_m=0.2236)
    lv = L.next_lever(L.pass_facts(1, 0, _q(cells=900_000), {**PLAN, "detail": {"ceiling": 625_000}}), [], PLAN)
    coarser = P.finish(RECORD, P.zones(RECORD), HIST, level=level, diag_m=0.2236, base_factor=L.base_factor([lv]))
    assert coarser["base_size_m"] > plain["base_size_m"]
    assert coarser["estimate"]["elements"] < plain["estimate"]["elements"]


class _Mesh:
    """Two faces meshed at known edge lengths: Face_3 (a 5 mm wall) and Face_7 (a 10 mm hole)."""

    def __init__(self, h3, h7):
        import numpy as np

        self.node_ids = np.arange(1, 7)
        self.xyz = np.array([[0, 0, 0], [h3, 0, 0], [0, h3, 0], [1, 1, 1], [1 + h7, 1, 1], [1, 1 + h7, 1]], float)
        self.faces = {3: np.array([[1, 2, 3]]), 7: np.array([[4, 5, 6]])}


def _rules(rec, h3, h7):
    from meshpipeline.engines import fea_checks

    plan = P.finish(rec, P.zones(rec), HIST, level=P.LEVELS["standard"], diag_m=0.2236)
    plan["base_size_m"] = 1.0                       # every zone binds
    return fea_checks.measure(_Mesh(h3, h7), plan)


def test_the_mesh_checks_hold_each_zone_to_its_rule():
    ok = _rules(RECORD, 2e-3, 1e-3)
    assert ok["short"] == [] and ok["walls_checked"] and ok["through_wall"]["min"] == 2.5
    thin_wall = _rules(RECORD, 4e-3, 1e-3)               # 5 mm / 4 mm = 1.25 through
    assert "W3" in thin_wall["short"]


def test_a_hot_spot_is_held_to_three_through_its_own_wall():
    rec = json.loads(json.dumps(RECORD))
    rec["hot_spots"][1]["local_thickness_mm"] = 3.0
    # 1.4 mm edges: 11.2 per quarter-turn, but only 2.1 through the 3 mm wall beside the hole
    r = _rules(rec, 2e-3, 1.4e-3)
    assert "H2" in r["short"]
    spot = r["hot_spots"]["short"][0]
    assert spot["through_wall"] == pytest.approx(2.14, abs=0.01) and spot["wall_needed"] == 3


def test_unmeasured_walls_never_pass_the_gate():
    from types import SimpleNamespace

    from meshpipeline.engines.gmsh.gates import _gate_resolution_floor

    def gate(fea):
        ctx = SimpleNamespace(manifest_or_load=lambda: {"quality": {"fea": fea}})
        return _gate_resolution_floor(ctx)[0]

    assert gate({"plan": True, "through_wall_min": 2.4, "rules": {"walls_checked": True}})
    assert not gate({"plan": True, "through_wall_min": 1.2, "rules": {"walls_checked": True}})
    assert not gate({"plan": True, "through_wall_min": None, "rules": {}})
    assert not gate({"plan": True, "through_wall_min": 3.0, "rules": {"walls_checked": False}})
