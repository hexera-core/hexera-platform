# Responsibility: Verify PLAN-cfd-7 - the near wake keeps 25 cells per wake length and reaches 1.5 wake lengths behind the body whatever the budget (the far wake is cut first, the near wake priced in), a coarse wall gets a finer wake than one level under it, and a box round the body holds 50 cells per wake length beside it.
# Boundaries: pure arithmetic over boxes; no mesher is run.
from __future__ import annotations

import pytest

from meshpipeline.engines import wake_region as WR


def _plan(budget: float, *, level: int = 3, base: float = 0.0128, body=((0.0, -0.05, -0.05), (0.1, 0.05, 0.05)),
          knobs=None):
    lo, hi = body
    L = hi[0] - lo[0]
    return WR.plan_wake(body_min=lo, body_max=hi, domain_min=(lo[0] - 5 * L, -1.0, -1.0),
                        domain_max=(hi[0] + 10 * L, 1.0, 1.0), flow_axis="+x", base_cell=base,
                        surface_level=level, budget_cells=budget, knobs=knobs)


def _cell_at(plan: WR.WakePlan, x: float) -> float | None:
    inside = [t for t in plan.tiers if t.box_min[0] <= x <= t.box_max[0]]
    return min(t.cell_m for t in inside) if inside else None


def test_a_tight_budget_never_cuts_the_near_wake():
    # before: the cuts halved every box's reach first - the near wake ended short of one body length and the
    # wake there was the background (the CFD board: 29 of 120 under 25 cells per length 1 L behind)
    p = _plan(10_000)
    assert p.active and p.near["level"] >= 1
    x1 = 0.1 + 1.0 * p.scale_m                                   # one wake length behind the trailing edge
    cell = _cell_at(p, x1)
    assert cell is not None and p.scale_m / cell >= WR.NEAR_WAKE_CELLS
    assert p.tiers[0].reach_m >= WR.NEAR_WAKE_REACH * p.scale_m * (1 - 1e-9)
    assert p.raised > 0 and "budget raised" in p.changes[-1]


def test_a_coarse_wall_gets_a_wake_finer_than_one_level_under_it():
    # the board's wing 00049962: wall cells L/31, so one level under the wall is 15.7 cells per length
    p = _plan(10_000_000, level=1, base=0.0128)                # wall 6.4 mm on a 100 mm body
    assert p.tiers[0].level == 1                                # the wall's own level, not 0
    assert p.scale_m / p.tiers[0].cell_m >= 15


def test_a_generous_budget_keeps_the_planned_wake():
    p = _plan(50_000_000, level=4)
    assert p.changes == [] and p.raised == 0 and len(p.tiers) == 3
    assert p.tiers[0].level == 3 and p.tiers[-1].reach_m == pytest.approx(4 * p.scale_m)
    assert p.tiers[0].reach_m == pytest.approx(1.5 * p.scale_m)      # the near wake past one length, with room


def test_a_coarser_wake_the_plan_chose_keeps_its_old_cuts():
    p = _plan(10_000, knobs={"levels_below_surface": 2})
    assert p.near == {} and p.raised == 0


def test_the_box_beside_the_body_holds_fifty_cells_per_wake_length():
    nb = WR.plan_near_body(body_min=(0, -0.15, -0.15), body_max=(0.06, 0.15, 0.15), domain_min=(-1, -1, -1),
                           domain_max=(1, 1, 1), scale_m=0.3, base_cell=0.0128, surface_level=3)
    assert nb is not None and 0.3 / nb.cell_m >= WR.NEAR_BODY_CELLS
    assert nb.box_min[1] == pytest.approx(-0.15 - 0.03) and nb.cells < 1e6
    # never finer than the wall
    nb = WR.plan_near_body(body_min=(0, 0, 0), body_max=(0.01, 0.01, 0.01), domain_min=(-1, -1, -1),
                           domain_max=(1, 1, 1), scale_m=0.01, base_cell=0.0128, surface_level=3)
    assert nb is not None and nb.level == 3
