# Responsibility: Verify a cfMesh external case is timed before it runs - the run gets the guard its planned
# cells give it up to a ceiling, a plan that cannot fit is made coarser or refused before it starts, and a
# run its guard stopped is a guard trip whatever it was doing (docs/RULES.md TIME-2, TIME-3).
# Boundaries: engines/cfmesh (cfmesh_runner run plan, native.untangle_stall) and the run_mesh verdict; no
# OpenFOAM runs.
from __future__ import annotations

import json

import pytest

import meshpipeline.settings.runtime as rtcfg
from meshpipeline.engines.cfmesh import cfmesh_runner as R
from meshpipeline.engines.cfmesh.native import untangle_stall
from meshpipeline.engines.registry import get_spec

BODY = [{"name": "body", "type": "wall"}, {"name": "farfield", "type": "farfield"}]


def _render(ws, *, area, wall, fit=True, domain=(14.0, 11.0, 11.0), layers=2):
    return R.render_cfmesh_case(ws, surface_file="geom.fms", wall_patch="body", patches=BODY,
                                body_bbox=None, L=1.0, domain_min=[0, 0, 0],
                                domain_max=list(domain), strategy={"wall_cell": wall,
                                                                   "n_layers": layers},
                                cell_budget=8_000_000, max_halvings=R.EXTERNAL_MAX_HALVINGS,
                                snap_wall=True, wall_area=area, fit_run=fit)


def test_the_estimate_counts_the_background_and_the_walls_shell_and_layers():
    est = R.estimate_cells(volume=1000.0, max_cell=1.0, wall_cell=0.01, wall_area=2.0, n_layers=2)
    assert est == 1000 + int(2.0 / 0.01 ** 2 * (R.EXTERNAL_SHELL_DEPTH + 2))
    assert R.estimate_cells(volume=1000.0, max_cell=1.0, wall_cell=0.01, wall_area=None,
                            n_layers=2) is None


def test_a_planned_run_gets_the_guard_its_planned_cells_give_between_the_base_cap_and_the_ceiling(
        monkeypatch):
    from meshpipeline.contracts.work_limits import FILL_RATES, GUARD_FACTOR
    monkeypatch.setattr(rtcfg, "OPENFOAM_COMMAND_TIMEOUT", 1500)
    assert R.base_run_cap_s() == 1200 and R.run_ceiling_s() == 3000
    assert R.run_cap_for(None) == 1200
    assert R.run_cap_for(20.0) == 1200                   # a quick run keeps the base cap
    # docs/RULES.md TIME-2: 3 x the planned cells at the slow rate, the rescue's runs counted
    cells = 100.0 * R.CELLS_PER_SECOND
    want = int(GUARD_FACTOR * cells / FILL_RATES["cfmesh"] * R.RESCUE_RUNS)
    assert 1200 < want < 3000 and R.run_cap_for(100.0) == want
    assert R.run_cap_for(9000.0) == 3000                 # never past the ceiling
    assert R.run_guard(cells).as_dict()["guard_s"] == want
    # whether a plan FITS one run is still a planning rule, decided before the run
    assert R.fits_one_run(2400.0) and not R.fits_one_run(2401.0)


def test_the_render_records_the_plan_and_the_run_reads_its_cap(tmp_path):
    out = _render(tmp_path, area=1.6, wall=0.004)
    plan = out["run_plan"]
    assert plan["estimated_cells"] > 0 and plan["fits"] is True
    assert plan["estimated_seconds"] == pytest.approx(plan["estimated_cells"] / R.CELLS_PER_SECOND,
                                                      abs=0.1)
    on_disk = json.loads((tmp_path / R.RUN_PLAN_FILE).read_text())
    assert on_disk["run_cap_s"] == plan["run_cap_s"] == R.planned_run_cap(tmp_path)
    # the run policy hands that cap to run_mesh
    assert get_spec("cfmesh").run_policy.planned_timeout(tmp_path) == plan["run_cap_s"]
    # a case rendered without a plan never runs on a plan written for another case
    R.render_cfmesh_case(tmp_path, surface_file="geom.fms", wall_patch="body", patches=BODY,
                         body_bbox=None, L=1.0, domain_min=[0, 0, 0], domain_max=[14, 11, 11],
                         strategy={"wall_cell": 0.004}, cell_budget=8_000_000)
    assert not (tmp_path / R.RUN_PLAN_FILE).exists() and R.planned_run_cap(tmp_path) is None


def test_a_plan_too_long_for_one_run_is_made_coarser_one_octree_level_at_a_time(tmp_path):
    # 40 m2 of wall at 2 mm: ~60M cells, hours of cfMesh - the wall steps up until the run fits
    out = _render(tmp_path, area=40.0, wall=0.002)
    plan = out["run_plan"]
    assert plan["fits"] is True and plan["coarsened_levels"] >= 1
    assert plan["cells_before"] > plan["estimated_cells"]
    assert R.fits_one_run(plan["estimated_seconds"])
    assert "levels coarser" in plan["note"] or "level coarser" in plan["note"]
    assert out["wall_cell_size"] <= 1.0 / 20.0
    # a wall the BUILDER chose is never changed behind its back - only timed
    kept = _render(tmp_path, area=40.0, wall=0.002, fit=False)
    assert "coarsened_levels" not in kept["run_plan"] and kept["run_plan"]["fits"] is False


def test_a_plan_that_cannot_fit_even_the_coarsest_wall_says_so():
    plan = R._plan_run(volume=1.0, max_cell=1e-4, wall_cell=0.05, wall_area=1.0, n_layers=2,
                       coarsest=0.05, fit=True)
    assert plan["fits"] is False and "more than one run may take" in plan["note"]


# a run that timed out still untangling broken cells (e2e sweep, 2026-10-17)

BWB_TAIL = """    Error in face pyramids: 11934 faces pointing the wrong way!
--> FOAM Warning :
    18402 faces with severe warpage(flatness < 0.8) found.
    40115 zero or negative part tetrahedra detected.
Iteration 8. Number of bad faces is 43771"""
SOCCER_TAIL = """    Error in face pyramids: 3 faces pointing the wrong way!
Iteration 8. Number of bad faces is 3
Iteration 1. Number of bad faces is 0
Finished untangling the mesh
Renumbering the mesh
Finished renaming boundary patches"""


def test_a_run_out_of_time_while_untangling_is_broken_and_a_slow_one_is_not():
    assert untangle_stall(BWB_TAIL) == [
        "43,771 bad faces cfMesh could not untangle, 11,934 of them pointing the wrong way"]
    assert untangle_stall(SOCCER_TAIL) == []
    assert untangle_stall("") == [] and untangle_stall("Iteration 2. Number of bad faces is 0") == []
    assert get_spec("cfmesh").run_policy.timed_out_defects(BWB_TAIL)


class _FakeCfMesh:
    name = "cfmesh"

    def __init__(self, res, fatal):
        self.res, self.fatal = res, fatal

    def run_cartesian_mesh(self, ws, *, timeout, context=None):
        self.timeout = timeout
        return dict(self.res)

    def check_mesh(self, ws):
        return {"cells": 10, "mesh_ok": not self.fatal, "fatal": list(self.fatal)}


def test_a_broken_mesh_is_not_run_again_when_the_run_will_move_on(tmp_path, monkeypatch):
    import meshpipeline.engines.runtime as rt
    from meshpipeline.agents.builder.tool_context import BuilderToolContext
    from meshpipeline.agents.builder.tools import meshing

    res = {"rc": 0, "timed_out": False, "log_tail": ""}
    monkeypatch.setattr(rt, "get_engine", lambda n="": _FakeCfMesh(res, ["negative-volume cells"]))
    prepared = meshing.PreparedMeshRun(engine="cfmesh", cap=60)
    moving = BuilderToolContext(workspace=tmp_path, geometry=None, engine="cfmesh",
                                broken_mesh_moves_to="snappy")
    out = meshing._run_and_judge(moving, prepared)
    assert out["success"] is False and out["moves_on_to"] == "snappy"
    assert "Do NOT run_mesh again" in out["guidance"] and out["fatal_defects"]
    staying = BuilderToolContext(workspace=tmp_path, geometry=None, engine="cfmesh")
    kept = meshing._run_and_judge(staying, prepared)
    assert "moves_on_to" not in kept and "Do NOT run_mesh again" not in kept["guidance"]


@pytest.mark.parametrize("moves_to", ["snappy", ""])
def test_a_run_stopped_by_its_guard_is_a_guard_trip_never_a_broken_mesh(tmp_path, monkeypatch,
                                                                        moves_to):
    # docs/RULES.md TIME-3: what a run was doing when its guard clock stopped it (still untangling
    # broken cells, here) depends on how busy the machine was - so it decides nothing: no move to
    # another engine, no "too fine", no broken-mesh refusal. The attempt ends and is replayed.
    import meshpipeline.engines.runtime as rt
    from meshpipeline.agents.builder.tool_context import BuilderToolContext
    from meshpipeline.agents.builder.tools import meshing
    from meshpipeline.engines import determinism as det
    from meshpipeline.engines.preflight import read_refusal

    res = {"rc": -1, "timed_out": True, "log_tail": BWB_TAIL}
    monkeypatch.setattr(rt, "get_engine", lambda n="": _FakeCfMesh(res, []))
    ctx = BuilderToolContext(workspace=tmp_path, geometry=None, engine="cfmesh",
                             broken_mesh_moves_to=moves_to)
    out = meshing._run_and_judge(ctx, meshing.PreparedMeshRun(engine="cfmesh", cap=60))
    assert out["success"] is False and out["guard_tripped"] is True
    assert "moves_on_to" not in out and "end your turn" in out["guidance"]
    assert read_refusal(tmp_path) is None
    trips = det.guard_trips(tmp_path)
    assert [t["where"] for t in trips] == ["cfmesh"] and trips[0]["rule"] == "TIME-3"
    assert "43,771 bad faces" in trips[0]["repairing"]          # words for the record only
    # ...and a second run_mesh in the same attempt is refused: the attempt ends on the trip
    assert meshing._attempt_tripped(tmp_path)
