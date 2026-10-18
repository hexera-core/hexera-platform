# Responsibility: Verify a cfMesh external case is timed before it runs - the run gets the time its plan
# needs up to a ceiling, a plan that cannot fit is made coarser or refused before it starts, and a run
# that timed out still untangling broken cells is read as the broken mesh it is.
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


def test_a_planned_run_gets_the_time_it_needs_between_the_base_cap_and_the_ceiling(monkeypatch):
    monkeypatch.setattr(rtcfg, "OPENFOAM_COMMAND_TIMEOUT", 1500)
    assert R.base_run_cap_s() == 1200 and R.run_ceiling_s() == 3000
    assert R.run_cap_for(None) == 1200
    assert R.run_cap_for(300.0) == 1200                  # a quick run keeps the base cap
    assert R.run_cap_for(1600.0) == int(1.25 * 1600)     # a long one gets its estimate and room
    assert R.run_cap_for(9000.0) == 3000                 # never past the ceiling
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


@pytest.mark.parametrize("res,fatal", [
    ({"rc": 0, "timed_out": False, "log_tail": ""}, ["negative-volume cells"]),
    ({"rc": -1, "timed_out": True, "log_tail": BWB_TAIL}, [])])
def test_a_broken_mesh_is_not_run_again_when_the_run_will_move_on(tmp_path, monkeypatch, res,
                                                                  fatal):
    import meshpipeline.engines.runtime as rt
    from meshpipeline.agents.builder.tool_context import BuilderToolContext
    from meshpipeline.agents.builder.tools import meshing

    monkeypatch.setattr(rt, "get_engine", lambda n="": _FakeCfMesh(res, fatal))
    prepared = meshing.PreparedMeshRun(engine="cfmesh", cap=60)
    moving = BuilderToolContext(workspace=tmp_path, geometry=None, engine="cfmesh",
                                broken_mesh_moves_to="snappy")
    out = meshing._run_and_judge(moving, prepared)
    assert out["success"] is False and out["moves_on_to"] == "snappy"
    assert "Do NOT run_mesh again" in out["guidance"] and out["fatal_defects"]
    staying = BuilderToolContext(workspace=tmp_path, geometry=None, engine="cfmesh")
    kept = meshing._run_and_judge(staying, prepared)
    assert "moves_on_to" not in kept and "Do NOT run_mesh again" not in kept["guidance"]


def test_the_run_verdict_of_a_stalled_run_is_a_broken_mesh_that_moves_the_run_on(tmp_path):
    from meshpipeline.agents.builder.tool_context import BuilderToolContext
    from meshpipeline.agents.builder.tools import meshing
    from meshpipeline.engines.preflight import read_refusal

    ctx = BuilderToolContext(workspace=tmp_path, geometry=None, engine="cfmesh",
                             broken_mesh_moves_to="snappy")
    stall = meshing._broken_at_time_out(get_spec("cfmesh").run_policy, BWB_TAIL)
    out = meshing._stalled_verdict(ctx, stall, BWB_TAIL)
    assert out["moves_on_to"] == "snappy" and "end your turn" in out["guidance"]
    assert "snappyHexMesh" in out["guidance"]
    rec = read_refusal(tmp_path)
    assert rec is not None and rec.cause == "mesh_quality" and rec.facts["fatal"] == stall
    # on an engine the user named there is nowhere to move: the builder is told what to change
    mine = meshing._stalled_verdict(BuilderToolContext(workspace=tmp_path, geometry=None,
                                                       engine="cfmesh"), stall, BWB_TAIL)
    assert "moves_on_to" not in mine and "boundaryLayers" in mine["guidance"]
