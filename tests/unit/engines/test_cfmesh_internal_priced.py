# Responsibility: Verify cfMesh prices every internal case before it runs and makes a plan over its cell budget
# coarser at the wall, said in plain words (engines/cfmesh/cfmesh_runner, rulebook 75).
# Boundaries: the meshDict and the run plan the writer authors; no mesher runs.
from __future__ import annotations

import json

import numpy as np

import meshpipeline.engines.cfmesh.cfmesh_runner as R

#: the CFD board's coronary tree (vmr 0257) as the port fallback read it: banded at its smallest outlet
PORTS = {"min": 0.000164, "p05": 0.000164, "median": 0.000164, "max": 0.00155, "points": 8, "source": "ports"}
WALL_M2 = 4.93e-3                                     # 4,932 mm2 of vessel wall


def _render(tmp_path, *, budget=8_000_000, **kw):
    ws = tmp_path
    (ws / "system").mkdir(parents=True, exist_ok=True)
    return R.render_cfmesh_case(
        ws, surface_file="geom.fms", wall_patch="wall",
        patches=[{"name": "wall", "type": "wall"}, {"name": "inlet", "type": "inlet"},
                 {"name": "outlet", "type": "outlet"}],
        body_bbox=([0.0, 0.0, 0.0], [0.26, 0.26, 0.26]), L=0.26, domain_min=[0.0, 0.0, 0.0],
        domain_max=[0.26, 0.26, 0.26], strategy={"n_layers": 2}, cell_budget=budget, **kw)


def test_an_internal_plan_over_its_budget_is_made_coarser_before_it_runs(tmp_path):
    out = _render(tmp_path, passage_radius=PORTS, internal_wall_area=WALL_M2)
    plan = json.loads((tmp_path / R.RUN_PLAN_FILE).read_text())
    assert plan["priced"] == "internal" and plan["estimated_cells"] <= 8_000_000
    assert plan["coarsened_levels"] >= 2 and plan["cells_before"] > 50_000_000, plan
    assert out["planned_cells"] == plan["estimated_cells"]
    note = out["budget_note"]
    assert note.startswith(f"The wall cells were made {plan['coarsened_levels']} levels coarser")
    assert "so the mesh fits the 8 M cells one job builds" in note and "ask for a cell count" in note
    dic = (tmp_path / "system" / "meshDict").read_text()
    assert f"wall {{ cellSize {out['wall_cell_size']:.4g}" in dic or "wall { cellSize" in dic


def test_an_internal_plan_inside_its_budget_is_priced_and_left_as_planned(tmp_path):
    wide = {**PORTS, "min": 0.004, "p05": 0.004, "median": 0.004}
    out = _render(tmp_path, passage_radius=wide, internal_wall_area=WALL_M2)
    plan = json.loads((tmp_path / R.RUN_PLAN_FILE).read_text())
    assert "coarsened_levels" not in plan and "budget_note" not in out
    assert 0 < plan["estimated_cells"] <= 8_000_000
    assert plan["run_cap_s"] >= R.base_run_cap_s(), "a priced plan never runs on less than the base cap"


def test_the_estimate_reads_the_fluid_never_the_box():
    est = R.internal_estimate(max_cell=1.6e-4, wall_cell=1.6e-4, thickness=1.3e-3, n_layers=2,
                              wall_area=WALL_M2, typical_radius=1.2e-3)
    # a tube round 4,932 mm2 of wall at 1.2 mm radius holds about 3 cm3: about 2 M cells at 0.16 mm,
    # where the 0.26 m box would be billions
    assert 5e5 < est < 5e6, est
    a = np.full(100, WALL_M2 / 100)
    r = np.full(100, 1.2e-3)
    on_field = R.internal_estimate(max_cell=1.6e-4, wall_cell=1.6e-4, thickness=1.3e-3, n_layers=2,
                                   areas=a, radius=r)
    assert 0.3 < on_field / est < 3.0
