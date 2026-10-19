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
    assert not plan.stops and plan.options() == []
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


def test_over_the_limit_the_run_stops_before_meshing_with_the_users_options():
    pts, r, areas, cell = _tree()
    at = _regions(pts, r, areas, cell)
    cost = G.box_cost(at(13.0), cell)
    base = 1000.0
    plan = G.plan_gaps(at, cell_m=cell, budget=int(base), limit=int(base + 0.3 * cost),
                       base_cells=base)
    assert plan.status == G.OVER_LIMIT and plan.stops and plan.regions == []
    assert plan.affordable is not None and G.MIN_ACCEPTED_CELLS <= plan.affordable < 13
    opts = plan.options()
    assert len(opts) == 3
    assert f"about {plan.affordable:.0f} cells across" in opts[0]
    assert "limit of one job raised" in opts[1]
    assert "smaller piece" in opts[2]
    ref = plan.refusal()
    assert ref.gate == "resolution_floor"
    from meshpipeline.contracts.failure_cause import rebuild_over_limit, retry_can_help
    assert rebuild_over_limit(ref.facts) and not retry_can_help(ref.cause, ref.facts)
    assert ref.facts["before_meshing"] is True


def test_accepting_fewer_meshes_what_the_limit_holds_and_the_gate_judges_that():
    pts, r, areas, cell = _tree()
    at = _regions(pts, r, areas, cell)
    cost = G.box_cost(at(13.0), cell)
    base = 1000.0
    plan = G.plan_gaps(at, cell_m=cell, budget=int(base), limit=int(base + 0.3 * cost),
                       base_cells=base, accept_fewer=True)
    assert plan.status == G.ACCEPTED_FEWER and plan.regions and not plan.stops
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
