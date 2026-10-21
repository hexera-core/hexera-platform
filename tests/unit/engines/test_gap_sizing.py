# Responsibility: Verify cells are sized from the measured narrowest gap - the rule everywhere, refined only at the narrow spots,
# and the cell limit checked BEFORE meshing, with the user's options when it cannot hold the rule.
# Round 4 of the lab sweep: 19 of 59 dev-side failures were meshes too coarse across their narrowest passage, every one after the
# narrow spots' boxes had been held to half the planned budget and the rest dropped - then meshed for up to an hour and refused.
from __future__ import annotations

import json

import numpy as np
import pytest

import meshpipeline.engines.gap_sizing as G
import meshpipeline.engines.passage as P


def _line(start, direction, n, spacing):
    d = np.asarray(direction, dtype=float)
    return np.asarray(start, dtype=float) + np.outer(np.arange(n) * spacing, d / np.linalg.norm(d))


def _tree(scale: float = 1.0):
    """A trunk (r = 50 mm) with a 5 mm branch, scaled; wall area per point included."""
    trunk = _line([0, 0, 0], [1, 0, 0], 200, 0.005 * scale)
    branch = _line([0.5 * scale, 0.06 * scale, 0], [0, 1, 0], 40, 0.0025 * scale)
    pts = np.concatenate([trunk, branch])
    r = np.concatenate([np.full(len(trunk), 0.05 * scale), np.full(len(branch), 0.005 * scale)])
    areas = np.concatenate([np.full(len(trunk), 1.6e-3 * scale ** 2),
                            np.full(len(branch), 8e-5 * scale ** 2)])
    cell = 2 * 0.05 * scale / 13
    return pts, r, areas, cell


def _regions(pts, r, areas, cell):
    def at(t):
        return P.narrow_passage_regions(pts, r, cell_m=cell, areas=areas, target=t, detect=13.0)
    return at


def test_the_rule_is_the_gates_own_count():
    assert G.GAP_CELLS_ACROSS == P.PASSAGE_CELLS_ACROSS == 13
    assert G.GAP_FLOOR_CELLS == P.PASSAGE_FLOOR_CELLS == 12


def test_the_same_rule_for_a_2_mm_channel_and_a_40_m_hull():
    small = G.plan_gaps(_regions(*_tree(0.02)), cell_m=_tree(0.02)[3], budget=10**9, limit=10**9)
    large = G.plan_gaps(_regions(*_tree(400.0)), cell_m=_tree(400.0)[3], budget=10**9, limit=10**9)
    assert small.status == large.status == G.FITS
    assert small.spots == large.spots and small.target == large.target == 13
    # the cost is a count of cells: the same shape at any size costs the same
    assert small.cost == pytest.approx(large.cost, rel=1e-6)
    assert small.narrowest_m == pytest.approx(2 * 0.005 * 0.02)
    assert large.narrowest_m == pytest.approx(2 * 0.005 * 400.0)


def test_only_the_narrow_spots_are_refined():
    pts, r, areas, cell = _tree()
    plan = G.plan_gaps(_regions(pts, r, areas, cell), cell_m=cell, budget=10**9, limit=10**9)
    assert plan.regions
    for b in plan.regions:
        assert b["max"][0] < 0.6 and b["min"][0] > 0.4, "no box reaches the trunk's ends"


def test_within_the_budget_nothing_changes():
    pts, r, areas, cell = _tree()
    at = _regions(pts, r, areas, cell)
    cost = G.box_cost(at(13.0), cell)
    plan = G.plan_gaps(at, cell_m=cell, budget=int(10 * cost), limit=int(100 * cost))
    assert plan.status == G.FITS and plan.budget == int(10 * cost)
    assert plan.ceiling() == int(10 * cost), "a plan that fits keeps its own ceiling"
    assert not plan.resolution_short and plan.options() == []
    assert "refining them locally to 13 across" in plan.words()


def test_over_the_budget_but_within_the_limit_the_budget_is_raised_never_the_spots_dropped():
    pts, r, areas, cell = _tree()
    at = _regions(pts, r, areas, cell)
    cost = G.box_cost(at(13.0), cell)
    plan = G.plan_gaps(at, cell_m=cell, budget=int(0.5 * cost), limit=int(100 * cost))
    assert plan.status == G.RAISED
    assert len(plan.regions) == len(at(13.0)), "every narrow spot keeps its box"
    # the plan's own share of its budget stays the base; the boxes come on top
    assert plan.budget >= (0.25 * cost + cost) * G.BUDGET_HEADROOM - 1
    assert "cell budget was raised" in plan.words() and "before meshing" in plan.words()


def test_over_the_limit_it_meshes_what_the_limit_holds_and_still_offers_the_choices():
    # the estimate is cautious: a refusal before meshing would fail files the gate passes (a
    # shell-side heat exchanger read 11.9 across at the limit and its plain mesh passed). So the
    # narrow spots are refined to what the limit holds and the mesh is held to the FULL floor.
    pts, r, areas, cell = _tree()
    at = _regions(pts, r, areas, cell)
    cost = G.box_cost(at(13.0), cell)
    base = 1000.0
    plan = G.plan_gaps(at, cell_m=cell, budget=int(base), limit=int(base + 0.3 * cost),
                       base_cells=base)
    assert plan.status == G.LIMITED and plan.regions and plan.resolution_short
    assert plan.affordable is not None and G.MIN_ACCEPTED_CELLS <= plan.affordable < 13
    assert plan.target == pytest.approx(plan.affordable)
    assert plan.budget == plan.ceiling() <= plan.limit
    assert plan.record()["resolution_short"] is True
    assert base + G.box_cost(plan.regions, cell) * G.BUDGET_HEADROOM <= plan.limit + 1
    assert plan.floor() == G.GAP_FLOOR_CELLS          # not lowered: nobody chose fewer
    assert "as many as the limit holds" in plan.words() and "still checked for 12" in plan.words()
    opts = plan.options()
    assert len(opts) == 3
    assert f"about {plan.affordable:.0f} cells across" in opts[0]
    assert "limit of one job raised" in opts[1]
    assert "smaller piece" in opts[2]


def test_when_not_even_the_fewest_cells_fit_the_run_still_meshes_as_planned_never_stops():
    # the shell-side heat exchanger (lab, 2026-10-21): on cfMesh its plain mesh nearly filled the
    # limit, so not even 4 across fit, and the run was stopped before meshing - a file snappyHexMesh
    # delivers at 6.5 M cells. Now the run meshes as before gap sizing and the record says why.
    pts, r, areas, cell = _tree()
    at = _regions(pts, r, areas, cell)
    base = 1000.0
    # a limit the base mesh alone fills: no count of cells across the narrow spots fits
    plan = G.plan_gaps(at, cell_m=cell, budget=int(base), limit=int(base), base_cells=base)
    assert plan.status == G.OVER_LIMIT and plan.regions == [] and plan.resolution_short
    assert not hasattr(plan, "stops") and not hasattr(plan, "refusal")
    assert plan.affordable is None and plan.ceiling() == int(base)
    words = plan.words()
    assert "not meshed" not in words and "refined only as far as the planned cell budget" in words
    assert "still checked for 12" in words
    opts = plan.options()
    assert len(opts) == 2 and "limit of one job raised" in opts[0] and "smaller piece" in opts[1]
    assert plan.record()["resolution_short"] is True


def test_the_wall_layers_are_priced_and_the_ceiling_leaves_room_for_them():
    # snappyHexMesh's ceiling stops its REFINEMENT; the wall layers come on top. Priced without
    # them the plan raised the ceiling to the limit: 7.1 M refined + 5.9 M layer cells = 13.0 M
    # on an 8 M limit (the shell-side heat exchanger, lab 2026-10-21).
    pts, r, areas, cell = _tree()
    at = _regions(pts, r, areas, cell)
    cost = G.box_cost(at(13.0), cell)
    budget, limit = int(0.5 * cost), int(4 * cost)
    bare = G.plan_gaps(at, cell_m=cell, budget=budget, limit=limit)
    assert bare.status == G.RAISED and bare.after_cells == 0.0
    layers = 2.5 * cost
    plan = G.plan_gaps(at, cell_m=cell, budget=budget, limit=limit, base_after=layers)
    # the same boxes no longer fit beside the layers: refined to what the limit holds instead
    assert plan.status == G.LIMITED and plan.base_after == layers
    assert plan.base_cells == pytest.approx(0.5 * budget + layers)
    assert plan.ceiling() + plan.after_cells <= limit / G.BUDGET_HEADROOM + 1
    assert plan.ceiling() < bare.ceiling()
    # a box's own wall layers count toward it, and toward the plan's
    boxes = at(13.0)
    for b in boxes:
        b["layer_cells"] = 10.0
    assert G.box_cost(boxes, cell) == pytest.approx(cost + 10.0 * len(boxes))


def test_the_ceiling_is_never_under_the_plans_own_budget():
    pts, r, areas, cell = _tree()
    at = _regions(pts, r, areas, cell)
    cost = G.box_cost(at(13.0), cell)
    # layers so large that the room left for refinement is under the plan's own budget
    plan = G.plan_gaps(at, cell_m=cell, budget=int(cost), limit=int(3 * cost),
                       base_cells=0.1 * cost, base_after=2.0 * cost)
    assert plan.status in (G.LIMITED, G.OVER_LIMIT)
    assert plan.ceiling() >= int(cost)


def test_accepting_fewer_meshes_what_the_limit_holds_and_the_gate_judges_that():
    pts, r, areas, cell = _tree()
    at = _regions(pts, r, areas, cell)
    cost = G.box_cost(at(13.0), cell)
    base = 1000.0
    plan = G.plan_gaps(at, cell_m=cell, budget=int(base), limit=int(base + 0.3 * cost),
                       base_cells=base, accept_fewer=True)
    assert plan.status == G.ACCEPTED_FEWER and plan.regions and plan.resolution_short
    assert base + G.box_cost(plan.regions, cell) * G.BUDGET_HEADROOM <= plan.limit + 1
    assert plan.floor() == pytest.approx(G.FLOOR_SHARE * plan.target)
    assert "as you chose" in plan.words()


def test_the_accepted_floor_is_read_only_for_an_approved_choice(tmp_path):
    rec = {"status": G.ACCEPTED_FEWER, "floor": 7.4}
    (tmp_path / G.GAP_RECORD).write_text(json.dumps(rec))
    assert G.accepted_floor(tmp_path, {G.NARROW_GAPS_PARAM: G.WITHIN_LIMIT}) == pytest.approx(7.4)
    # without the approved choice the rule's floor stands, whatever the record says
    assert G.accepted_floor(tmp_path, {}) is None
    assert G.accepted_floor(tmp_path, {G.NARROW_GAPS_PARAM: G.FULL_RULE}) is None
    (tmp_path / G.GAP_RECORD).write_text(json.dumps({**rec, "status": G.RAISED}))
    assert G.accepted_floor(tmp_path, {G.NARROW_GAPS_PARAM: G.WITHIN_LIMIT}) is None


def test_no_narrow_spot_no_plan():
    pts = _line([0, 0, 0], [1, 0, 0], 100, 0.01)
    r = np.full(100, 0.05)
    at = _regions(pts, r, np.full(100, 1e-4), 2 * 0.05 / 13)
    plan = G.plan_gaps(at, cell_m=2 * 0.05 / 13, budget=2_000_000, limit=8_000_000)
    assert plan.status == G.NO_NARROW and plan.words() == "" and plan.budget == 2_000_000


def test_the_estimate_at_approval_names_the_choice_only_over_the_limit():
    class T:
        gap_cells = 3.0e6
        narrowest_gap = 0.0026
        narrowest_gap_rel = 0.01
    est = G.estimate_from_traits(T(), limit=8_000_000)
    assert est is not None and not est.over_limit
    assert "2.6 mm" in est.sentence() and "inside the 8.0 M-cell limit" in est.sentence()
    T.gap_cells = 2.0e7
    est = G.estimate_from_traits(T(), limit=8_000_000)
    assert est.over_limit
    s = est.sentence()
    assert "more than the 8.0 M" in s and "smaller piece" in s and "cell limit raised" in s
    # 2e7 x 1.15 against 8e6: the rule's cost goes with the cube of the count, so ~9 across
    assert est.affordable == pytest.approx(13 * (8e6 / (1.15 * 2e7)) ** (1 / 3))
    assert f"about {est.affordable:.0f} or more" in s
    assert G.WITHIN_LIMIT in est.guidance() and G.NARROW_GAPS_PARAM in est.guidance()
    # a recommendation carries the traits as a dict
    assert G.estimate_from_traits({"gap_cells": 2.0e7}, limit=8_000_000).over_limit
    T.gap_cells = None
    assert G.estimate_from_traits(T(), limit=8_000_000) is None


# EXTERNAL FLOW: the gaps between the parts of a body (a wheel and its arch, a flap and its slot)

def _two_blocks(gap: float):
    """Two 100 mm blocks side by side, `gap` metres apart, as triangles in metres."""
    def block(x0):
        x1, y, z = x0 + 0.1, 0.1, 0.1
        v = np.array([[x0, 0, 0], [x1, 0, 0], [x1, y, 0], [x0, y, 0],
                      [x0, 0, z], [x1, 0, z], [x1, y, z], [x0, y, z]], dtype=float)
        quads = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (2, 3, 7, 6), (1, 2, 6, 5), (0, 4, 7, 3)]
        return [v[[a, b, c]] for a, b, c, d in quads] + [v[[a, c, d]] for a, b, c, d in quads]
    return np.asarray(block(0.0) + block(0.1 + gap))


def test_the_gap_between_two_bodies_is_boxed_and_one_body_alone_is_not():
    from meshpipeline.cad.shape_traits import body_gap_field
    from meshpipeline.engines.snappy.snappy_runner import _plan_body_gaps
    field = body_gap_field(_two_blocks(0.002), samples=4000)
    assert field is not None
    _pts, half, _areas = field
    assert float(half[np.isfinite(half)].min()) == pytest.approx(0.001, rel=0.05)
    plan, ceiling = _plan_body_gaps(field, wall_cell=0.005, budget=2_000_000, base_cells=1e6)
    assert plan is not None and plan.regions, "a 2 mm slot at a 5 mm wall cell is boxed"
    for b in plan.regions:                    # only between the blocks
        assert 0.09 < b["min"][0] and b["max"][0] < 0.115
    assert ceiling >= 2_000_000
    assert "Gaps between parts" in plan.words(0.005, auto=True)
    assert body_gap_field(_two_blocks(0.002)[:12], samples=2000) is None, "one body: no gap"


# SNAPPY REFINES A NARROW SPOT BY DISTANCE FROM ITS OWN WALL, NOT BY A BOX ROUND IT

def test_a_narrow_spot_is_refined_by_distance_from_its_own_wall_never_as_a_patch(tmp_path):
    from meshpipeline.cad.stl_io import read_stl_triangles
    from meshpipeline.engines.snappy import drivers as D
    from meshpipeline.engines.snappy.snappy_runner import render_internal_case
    # a strip of wall: 40 points along x, two rows; the first half bounds a 2 mm passage
    xs = np.linspace(0.0, 0.2, 40)
    pts = np.concatenate([np.c_[xs, np.zeros(40), np.zeros(40)],
                          np.c_[xs, np.full(40, 0.005), np.zeros(40)]])
    faces = np.array([[i, i + 1, 40 + i] for i in range(39)]
                     + [[i + 1, 41 + i, 40 + i] for i in range(39)])
    r = np.where(pts[:, 0] < 0.1, 0.001, 0.02)
    areas = np.full(len(pts), 2.5e-5)
    field = (pts, r, areas, faces)
    plan = D._gap_plan(field, wall_cell=0.002, target=13.0, budget=2_000_000, band_m=0.0,
                       near_cell=0.002, surface_cell=0.002, base_cell=0.008,
                       accept_fewer=False)
    assert plan.regions and all("refine_m3" in b for b in plan.regions)
    ws = tmp_path / "case"
    (ws / "system").mkdir(parents=True)
    surfaces, boxes = D._narrow_surfaces(ws, list(plan.regions), field)
    assert surfaces and not boxes
    stl = ws / "constant" / "triSurface" / surfaces[0]["file"]
    tris = np.asarray(read_stl_triangles(stl)).reshape(-1, 3, 3)
    assert len(tris) and tris[:, :, 0].max() <= 0.1 + 0.2 / 39 + 1e-9, "the narrow wall only"
    assert surfaces[0]["distance_m"] == pytest.approx(D._REACH_MARGIN * 0.001)
    render_internal_case(
        ws, names={"wall": "wall", "inlet": "inlet"},
        features={"wall": "w.eMesh", "inlet": "i.eMesh"}, interior_point=(0, 0, 0),
        bbox_min=(-0.1, -0.1, -0.1), bbox_max=(0.1, 0.1, 0.1), base_cell=0.01,
        surface_level=2, feature_level=3, n_layers=0, wall_key="wall",
        distance_regions=surfaces)
    text = (ws / "system" / "snappyHexMeshDict").read_text()
    name = surfaces[0]["name"]
    assert f'{name} {{ type triSurfaceMesh; file "refine/{name}.stl"; name {name}; }}' in text
    assert f"{name} {{ mode distance; levels ((" in text
    surfaces_block = text.split("refinementSurfaces")[1].split("resolveFeatureAngle")[0]
    assert name not in surfaces_block, "a refinement surface, never a patch"
    assert f"constant/triSurface/refine/{name}.stl" in D._native_payload_members(ws)


def _strip_field():
    xs = np.linspace(0.0, 0.2, 40)
    pts = np.concatenate([np.c_[xs, np.zeros(40), np.zeros(40)],
                          np.c_[xs, np.full(40, 0.005), np.zeros(40)]])
    faces = np.array([[i, i + 1, 40 + i] for i in range(39)]
                     + [[i + 1, 41 + i, 40 + i] for i in range(39)])
    r = np.where(pts[:, 0] < 0.1, 0.001, 0.02)
    return pts, r, np.full(len(pts), 2.5e-5), faces


def test_the_snappy_gap_plan_prices_the_wall_layers_it_adds():
    from meshpipeline.engines.snappy import drivers as D
    field = _strip_field()
    kw = {"wall_cell": 0.002, "target": 13.0, "budget": 2_000_000, "band_m": 0.0,
          "near_cell": 0.002, "surface_cell": 0.002, "base_cell": 0.008, "accept_fewer": False}
    bare = D._gap_plan(field, **kw)
    plan = D._gap_plan(field, n_layers=5, **kw)
    wall = float(field[2].sum())
    # one wall face per wall cell squared, one layer cell per face per layer
    assert plan.base_after == pytest.approx(5 * wall / 0.002 ** 2)
    assert bare.base_after == 0.0
    assert plan.regions and all(b["layer_cells"] > 0.0 for b in plan.regions)
    assert plan.cost > bare.cost
    # a pass held to a lower limit (after a raised mesh came out over the limit) is planned on it
    held = D._gap_plan(field, n_layers=5, limit=1_000, **kw)
    assert held.limit == 1_000 and held.ceiling() == 2_000_000 and held.status == G.OVER_LIMIT


def test_a_retry_after_a_raised_mesh_over_the_limit_keeps_the_plans_own_budget(tmp_path):
    from meshpipeline.engines.snappy import drivers as D
    prev, cur = tmp_path / "attempt_1", tmp_path / "attempt_2"
    prev.mkdir()
    cur.mkdir()
    rec = {"status": G.LIMITED, "budget_in": 2_000_000, "budget": 3_000_000,
           "ceiling": 3_000_000}
    (prev / G.GAP_RECORD).write_text(json.dumps(rec))
    (prev / "mesh_manifest.json").write_text(json.dumps({"cell_count": 12_965_976}))
    assert D._raised_over_limit(cur) == 2_000_000
    # inside the limit, or a ceiling the gap plan did not raise: nothing to hold
    (prev / "mesh_manifest.json").write_text(json.dumps({"cell_count": 6_542_645}))
    assert D._raised_over_limit(cur) is None
    (prev / "mesh_manifest.json").write_text(json.dumps({"cell_count": 12_965_976}))
    (prev / G.GAP_RECORD).write_text(json.dumps({**rec, "budget": 2_000_000,
                                                  "ceiling": 2_000_000}))
    assert D._raised_over_limit(cur) is None
    assert D._raised_over_limit(prev) is None, "a first attempt has no sibling"


def test_cfmesh_never_stops_before_meshing_over_the_limit(tmp_path):
    from meshpipeline.engines.cfmesh.cfmesh_runner import render_cfmesh_case
    field = _strip_field()
    ws = tmp_path / "case"
    ws.mkdir()
    out = render_cfmesh_case(
        ws, surface_file="geom.fms", wall_patch="wall", patches=[], body_bbox=((0, 0, 0), (0.2, 0.005, 0.01)),
        L=0.2, domain_min=(0, 0, 0), domain_max=(0.2, 0.005, 0.01), strategy={"n_layers": 3},
        cell_budget=50, passage_radius={"p05": 0.001, "median": 0.02, "band": 0.02},
        passage_field=field[:3])
    assert "gap_stops" not in out
    assert (ws / "system" / "meshDict").is_file()
    rec = json.loads((ws / G.GAP_RECORD).read_text())
    assert rec["status"] == G.OVER_LIMIT and rec["resolution_short"] is True


def test_the_fewer_cells_choice_is_offered_only_for_engines_that_take_it():
    est = G.estimate_from_traits({"gap_cells": 2.0e7}, limit=8_000_000)
    assert est is not None and est.over_limit
    words = est.guidance(("snappyHexMesh", "cfMesh"))
    assert "only for snappyHexMesh and cfMesh" in words and G.WITHIN_LIMIT in words
    none = est.guidance(())
    assert G.WITHIN_LIMIT not in none and "leave narrow_gaps out" in none


def test_the_external_gap_plan_prices_the_wall_layers_too():
    from meshpipeline.cad.shape_traits import body_gap_field
    from meshpipeline.engines.snappy.snappy_runner import _plan_body_gaps
    field = body_gap_field(_two_blocks(0.002), samples=4000)
    bare, _ = _plan_body_gaps(field, wall_cell=0.005, budget=2_000_000, base_cells=1e6)
    plan, ceiling = _plan_body_gaps(field, wall_cell=0.005, budget=2_000_000, base_cells=1e6,
                                    n_layers=5, wall_area=0.12)
    assert bare.base_after == 0.0 and plan.base_after == pytest.approx(5 * 0.12 / 0.005 ** 2)
    assert plan.regions and all(b.get("layer_cells", 0.0) > 0.0 for b in plan.regions)
    assert ceiling >= 2_000_000
    assert ceiling + plan.after_cells <= plan.limit / G.BUDGET_HEADROOM + 1 or ceiling == 2_000_000
