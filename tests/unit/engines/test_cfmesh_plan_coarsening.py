# Responsibility: Verify a cfMesh internal plan over its budget is made coarser at the wall only where that buys
# cells and leaves the wall usable, its narrow-gap boxes are held as they are priced, and a plan still past the
# standard machine runs where its memory fits (engines/cfmesh/cfmesh_runner, engines/passage, PLAN-cfd-8).
# Boundaries: pure planning functions; no mesher runs.
from __future__ import annotations

import numpy as np

import meshpipeline.engines.cfmesh.cfmesh_runner as R

#: abc 00063401's internal plan: 210 M cells, 209.3 M of them in 114 narrow-gap boxes priced at their own
#: cells; eight levels at the wall (0.85 mm -> 218 mm) cut the estimate to 209 M and left 0.06 cells across
W0 = 0.00085


def test_a_count_the_wall_does_not_carry_keeps_the_wall():
    w, m, est, levels = R.coarser_wall(lambda w_, m_: 209.3e6 + 0.58e6 * (W0 / w_) ** 2, est=209.9e6,
                                       wall_cell=W0, max_cell=W0, limit=8e6, median_radius=0.0064)
    assert levels == 0 and w == W0 and est == 209.9e6


def test_a_count_the_wall_carries_is_still_made_coarser_until_it_fits():
    w, m, est, levels = R.coarser_wall(lambda w_, m_: 85e6 * (W0 / w_) ** 3, est=85e6,
                                       wall_cell=W0, max_cell=W0, limit=8e6)
    assert levels == 2 and est <= 8e6 and m >= w


def test_the_wall_never_leaves_fewer_than_two_cells_across_the_typical_passage():
    w, m, est, levels = R.coarser_wall(lambda w_, m_: 1e12 * (W0 / w_) ** 3, est=1e12,
                                       wall_cell=W0, max_cell=W0, limit=8e6, median_radius=0.0064)
    assert 2.0 * 0.0064 / w >= R.USABLE_WALL_CELLS_ACROSS and levels == 2, (w, levels)


def test_narrow_boxes_held_to_a_budget_are_held_at_their_whole_volume():
    from meshpipeline.engines.gap_sizing import box_cost
    from meshpipeline.engines.passage import narrow_passage_regions
    # narrow wall points strewn along a long curve: each box is far bigger than the gap its points bound
    t = np.linspace(0.0, 1.0, 400)
    pts = np.stack([0.1 * t, 0.08 * np.sin(6.0 * t), 0.05 * t ** 2], axis=1)
    r = np.full(len(t), 0.0004)
    a = np.full(len(t), 1e-7)
    kw = {"cell_m": 0.002, "areas": a, "target": 15.0, "detect": 14.5, "budget_cells": 1e5}
    held = narrow_passage_regions(pts, r, whole_box=True, **kw)
    assert box_cost(held, 0.002, whole_box=True) <= 1e5
    fluid = narrow_passage_regions(pts, r, **kw)
    assert box_cost(fluid, 0.002, whole_box=True) > 1e5, "held at the fluid, the whole boxes overrun the budget"


def test_a_plan_past_the_standard_machine_runs_where_its_memory_fits(tmp_path):
    from meshpipeline.contracts import mesh_machine as MM
    cells = 4 * MM.normal_cells()
    d = R.machine_for_plan(tmp_path, {"estimated_cells": cells, "estimated_seconds": 60.0, "over_limit": True})
    assert d.get("where") == "gce", d
    assert MM.cells_carried(d) >= min(cells, MM.ceiling_cells()), d
