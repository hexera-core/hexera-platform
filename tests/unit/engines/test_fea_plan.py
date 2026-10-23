# Responsibility: Verify the FEA plan's rules as pure arithmetic over a measurement record: the zones each rule asks for, the base size (the coarsest the part allows), the estimate against the element limit, the ladder's levers and the delivered pass, and that the same inputs give the same plan.
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


def test_a_tightened_zone_is_finer_by_the_factor():
    assert _zone(P.zones(RECORD, tighten={"W3": 0.8}), "W3")["size_m"] == pytest.approx(2e-3)


def test_zone_groups_never_coarsen_a_zone():
    zs = P.zones(RECORD)
    for z in zs:
        g = next(g for g in group_zones(zs) if (z["kind"] == "corner") == g["edge"] and
                 set(z["faces"]) <= set(g["faces"]) and g["size"] <= z["size_m"] < g["size"] * 1.2001)
        assert g["reach"] >= z["reach_m"]


HIST = Histogram(rows=[(2e-3, 1e-6), (math.inf, 9e-5)])   # 100 cm3, 1 cm3 of it in a 2 mm zone


def test_the_base_size_is_the_coarsest_the_part_allows_and_the_rules_refine():
    plan = P.finish(RECORD, P.zones(RECORD), HIST, diag_m=0.2236, limit=P.ELEMENT_LIMIT)
    assert plan["base_size_m"] == pytest.approx(0.2236 / P.MIN_DIVISIONS, rel=1e-5)
    assert plan["estimate"]["elements"] == round(elements(HIST, plan["base_size_m"], P.PER_H3))
    assert plan["fits"] and plan["limit"]["elements"] == P.ELEMENT_LIMIT
    assert plan["words"][0].startswith("The mesh the rules recommend")
    rules = {d["rule"] for d in plan["decisions"]}
    assert {"PLAN-fea-1", "PLAN-fea-14", "PLAN-fea-3", "PLAN-fea-4", "PLAN-fea-5", "PLAN-fea-6",
            "PLAN-fea-8"} <= rules
    assert "PLAN-fea-2" not in rules                     # retired with the detail levels


def test_over_the_limit_the_plan_does_not_fit_and_never_says_stop():
    heavy = Histogram(rows=[(1e-4, 1e-7), (math.inf, 9e-5)])     # 0.1 cm3 at 0.1 mm: ~450k on its own
    plan = P.finish(RECORD, P.zones(RECORD), heavy, diag_m=0.2236, limit=100_000)
    assert not plan["fits"]
    assert not any("Stopped" in w or "Option" in w for w in plan["words"])


def test_the_same_inputs_give_the_same_plan():
    a = P.finish(RECORD, P.zones(RECORD), HIST, diag_m=0.2236, limit=P.ELEMENT_LIMIT)
    b = P.finish(json.loads(json.dumps(RECORD)), P.zones(RECORD), Histogram.from_list(HIST.as_list()),
                 diag_m=0.2236, limit=P.ELEMENT_LIMIT)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_an_assembly_sizes_each_part_by_its_own_base():
    record = {"faces": {}, "part_bases": [
        {"n": 1, "id": "Part_1", "name": "Flange", "diag_mm": 200.0, "faces": ["Part_1_Face_1"]},
        {"n": 2, "id": "Part_2", "name": "", "diag_mm": 20.0, "faces": ["Part_2_Face_1", "Part_2_Face_2"]}]}
    zs = {z["id"]: z for z in P.zones(record)}
    assert zs["P1"]["size_m"] == pytest.approx(0.02) and zs["P2"]["size_m"] == pytest.approx(0.002)
    assert zs["P2"]["faces"] == ["Part_2_Face_1", "Part_2_Face_2"] and zs["P2"]["rule"] == "PLAN-fea-16"
    words = P.assembly_words({"parts": [{"id": "Part_1", "name": "Flange"}, {"id": "Part_2", "name": ""}],
                              "mode": "contact", "pairs": 1, "notes": ["Part_2 is small."]})
    assert words[0].startswith("An assembly of 2 parts: Part_1 (Flange), Part_2.")
    assert "contact pairs" in words[1] and words[2] == "Part_2 is small."
    assert P.assembly_words({}) == []


# #
# the ladder
# #

PLAN = {"limit": {"elements": 8_000_000}, "estimate": {"elements": 85_000}}


def _q(**kw):
    q = {"cells": 90_000, "min_sicn": 0.4, "fatal": [], "fea": {"rules": {"short": []}}}
    q.update(kw)
    return q


def test_the_first_failure_pulls_its_lever_once():
    assert L.next_lever(L.pass_facts(1, 3, {}, PLAN), [], PLAN)["lever"] == "LAD-fea-2"
    # A VALID MESH is the only failure: under the 0.1 floor is a note, a degenerate element pulls LAD-fea-1
    low = L.pass_facts(1, 0, _q(min_sicn=0.02), PLAN)
    assert low["valid"] and L.next_lever(low, [], PLAN) is None
    bad = L.pass_facts(1, 0, _q(min_sicn=0.0, fatal=["degenerate elements (SICN <= 0)"]), PLAN)
    lever = L.next_lever(bad, [], PLAN)
    assert lever["lever"] == "LAD-fea-1"
    assert L.next_lever(bad, [lever], PLAN) is None
    # a plan coarsened to fit is short where it says: no tightening
    coarse = {**PLAN, "coarsened": {"needed": 1}}
    assert L.next_lever(L.pass_facts(1, 0, _q(fea={"rules": {"short": ["W3"]}}), coarse), [], coarse) is None
    short = L.pass_facts(1, 0, _q(fea={"rules": {"short": ["W3"]}}), PLAN)
    lv = L.next_lever(short, [], PLAN)
    assert lv["lever"] == "LAD-fea-3" and lv["zones"] == ["W3"]
    assert L.next_lever(L.pass_facts(1, 0, _q(), PLAN), [], PLAN) is None


def test_the_ladder_stops_at_three_passes():
    bad = L.pass_facts(3, 0, _q(min_sicn=0.0, fatal=["degenerate"]), PLAN)
    assert L.next_lever(bad, [{"lever": "LAD-fea-2"}, {"lever": "LAD-fea-3", "zones": []}], PLAN) is None


def test_levers_change_only_their_own_settings():
    levers = [{"lever": "LAD-fea-1"}, {"lever": "LAD-fea-3", "zones": ["W3"]}]
    assert L.growth(levers) == L.GENTLE_GROWTH
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


def test_a_mesh_that_came_out_too_big_is_still_worded_and_retried():
    from meshpipeline.contracts.failure_cause import FailureCause, describe, retry_can_help

    assert retry_can_help(FailureCause.CELL_BUDGET, {"cells": 9_000_000, "limit": 8_000_000})
    assert describe(FailureCause.CELL_BUDGET, {"cells": 9_000_000, "limit": 8_000_000})[0].startswith(
        "The mesh came out too big")


class _Mesh:
    """Two faces meshed at known edge lengths: Face_3 (a 5 mm wall) and Face_7 (a 10 mm hole)."""

    def __init__(self, h3, h7):
        import numpy as np

        self.node_ids = np.arange(1, 7)
        self.xyz = np.array([[0, 0, 0], [h3, 0, 0], [0, h3, 0], [1, 1, 1], [1 + h7, 1, 1], [1, 1 + h7, 1]], float)
        self.faces = {3: np.array([[1, 2, 3]]), 7: np.array([[4, 5, 6]])}


def _rules(rec, h3, h7):
    from meshpipeline.engines import fea_checks

    plan = P.finish(rec, P.zones(rec), HIST, diag_m=0.2236, limit=P.ELEMENT_LIMIT)
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


def test_the_walls_are_a_note_never_a_gate():
    from types import SimpleNamespace

    from meshpipeline.engines.gmsh.gates import _gate_resolution_floor

    def gate(fea):
        ctx = SimpleNamespace(manifest_or_load=lambda: {"quality": {"fea": fea}})
        return _gate_resolution_floor(ctx)[0]

    # A VALID MESH is the only blocker (Areen, 21 Oct): through-wall counts are notes on the delivery
    assert gate({"plan": True, "through_wall_min": 2.4, "rules": {"walls_checked": True}})
    assert gate({"plan": True, "through_wall_min": 1.2, "rules": {"walls_checked": True}})
    assert gate({"plan": True, "through_wall_min": None, "rules": {}})


# ------------------------------------------------------------- LAD-fea-6: no identical retry ----
def test_the_surfaces_a_failed_fill_named_are_meshed_the_other_way_next():
    from meshpipeline.engines.gmsh.fea_adapter import BAD_SURFACES, surfaces_named

    assert surfaces_named("Invalid boundary mesh (overlapping facets) on surface 8 surface 8") == [8]
    log = f"Warning : No elements in surface 9\n{BAD_SURFACES} 8,9\nTraceback (most recent call last):"
    failed = L.pass_facts(1, 1, {}, PLAN, log=log)
    assert not failed["built"] and failed["bad_surfaces"] == [8, 9]
    lever = L.next_lever(failed, [], PLAN)
    assert lever["lever"] == "LAD-fea-2" and lever["surfaces"] == [8, 9] and "MeshAdapt" in lever["words"]
    assert L.mesh_settings([lever])["remesh_surfaces"] == [8, 9]
    # the same surfaces again: nothing new to change, so no identical pass
    assert L.next_lever(L.pass_facts(2, 1, {}, PLAN, log=log), [lever], PLAN) is None
    # a new surface after LAD-fea-2: LAD-fea-6 on its own
    again = L.pass_facts(2, 1, {}, PLAN, log=f"{BAD_SURFACES} 9,12")
    six = L.next_lever(again, [lever], PLAN)
    assert six["lever"] == "LAD-fea-6" and six["surfaces"] == [12]
    assert L.mesh_settings([lever, six])["remesh_surfaces"] == [8, 9, 12]


def test_an_invalid_element_s_surfaces_ride_with_the_gentler_pass():
    bad = L.pass_facts(1, 0, _q(min_sicn=0.0, fatal=["degenerate elements (SICN <= 0)"], fea_bad_surfaces=[4]), PLAN)
    lever = L.next_lever(bad, [], PLAN)
    assert lever["lever"] == "LAD-fea-1" and lever["surfaces"] == [4]
    assert L.mesh_settings([lever]) == {"algorithm3d": "delaunay", "optimise_passes": L.OPTIMISE_PASSES,
                                       "remesh_surfaces": [4]}
    # a valid mesh names no bad surface
    assert L.pass_facts(1, 0, _q(fea_bad_surfaces=[4]), PLAN)["bad_surfaces"] == []


def test_a_distance_field_never_samples_past_its_point_budget(monkeypatch):
    """fea0260: a perforated plate's 1,467 faces were sampled at the 300 x 300 its largest face needs -
    132M points, out of memory before any surface was meshed. Past the budget each face is sampled at
    its own size, in fields of at most MAX_FIELD_POINTS points."""
    from types import SimpleNamespace

    from meshpipeline.engines.gmsh import fea_adapter as A

    boxes = {1: (0, 0, 0, 0.5, 0.5, 0.001)} | dict.fromkeys(range(2, 1500), (0, 0, 0, 0.003, 0.003, 0.001))
    gm = SimpleNamespace(model=SimpleNamespace(getBoundingBox=lambda _d, t: boxes[t]))
    few = A.sampled_parts(gm, [2, 3, 4], 0.0008)
    assert few == [([2, 3, 4], A._sampling(gm, [2, 3, 4], 0.0008))]           # within budget: as before
    parts = A.sampled_parts(gm, sorted(boxes), 0.0008)
    assert sorted(t for p, _n in parts for t in p) == sorted(boxes)
    assert all(len(p) * n * n <= A.MAX_FIELD_POINTS or len(p) == 1 for p, n in parts)
    assert next(n for p, n in parts if 1 in p) == A.SAMPLING_MAX


def test_a_pass_killed_by_the_memory_limit_is_not_tried_again():
    killed = L.pass_facts(1, -9, {}, PLAN, log="[NATIVE SIGNAL] python was killed by SIGKILL", killed=True)
    assert killed["out_of_memory"] and L.next_lever(killed, [], PLAN) is None
    assert L.next_lever(L.pass_facts(1, 1, {}, PLAN), [], PLAN)["lever"] == "LAD-fea-2"
