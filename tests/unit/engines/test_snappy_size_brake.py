# Responsibility: Verify the snappy size brake - a deterministic stop on snappyHexMesh's own cell
# counts, not a clock: a run headed past SIZE_BRAKE_MARGIN times its budget (castellated, or with its
# layers before they are added) is stopped and its next pass planned with the budget cut by how far
# over it was headed; a run within its plan meshes as before; and the plan itself now counts the
# layer cells a large thin plate adds.
# Boundaries: synthetic snappy logs and a real subprocess printing them; no OpenFOAM.
from __future__ import annotations

import subprocess
import sys

import pytest

from meshpipeline.engines import size_brake as SB
from meshpipeline.engines.snappy.planner import coarsen_after_timeout
from meshpipeline.engines.snappy.snappy_runner import castellation_budget, final_estimate
from meshpipeline.sandbox.safe_exec import StoppedByWatch, run_guarded

A2_LOG = """Refined mesh : cells:2001224  faces:6700000  points:2300000  unbalance:0.0099
Mesh refined in = 812.3 s.
Snapped mesh : cells:2001224  faces:6600000  points:2200000  unbalance:0.0099
Before layer addition : cells:2001224  faces:6600000  points:2200000  unbalance:0.0099
Ideal layer addition : cells:1.30768e+07  unbalance:0.007
"""
WITHIN_LOG = """Refined mesh : cells:1604925  faces:5000000  points:1800000  unbalance:0.0099
Ideal layer addition : cells:2.02206e+06  unbalance:0.0276
Layer mesh : cells:2017113  faces:6000000  points:2200000  unbalance:0.0099
"""


def test_the_margin_and_target_are_the_measured_ones():
    assert SB.SIZE_BRAKE_MARGIN == 1.5 and SB.SIZE_BRAKE_TARGET == 0.9
    assert SB.SIZE_BRAKE_LAYER_MARGIN == 2.0


def test_a_with_layers_estimate_a_good_plate_pass_reads_is_not_stopped(tmp_path):
    """A2.SPlate at a 1M castellation budget: snappy's with-layers estimate read 3,403,690 cells,
    1.70x the 2M plan, and the pass finished production-grade at 2,776,512 (main, lab 25 Oct). At a
    1.5x layer margin the brake stopped it and cut the plate to 0.54M (lab, cfdtime-brake3)."""
    log = tmp_path / "snappyHexMesh.log"
    log.write_text("Refined mesh : cells:942000  faces:1\nIdeal layer addition : cells:3.40369e+06\n")
    brake = SB.SizeBrake(log, 2_000_000)
    assert brake() is None and brake.tripped is None


def test_a_castellated_mesh_just_past_one_and_a_half_times_still_trips(tmp_path):
    log = tmp_path / "snappyHexMesh.log"
    log.write_text("Refined mesh : cells:3484835  faces:1\n")     # A1.FlatPlate's first pass
    brake = SB.SizeBrake(log, 2_000_000)
    assert brake() and brake.tripped["stage"] == "castellated" and brake.tripped["margin"] == 1.5


def test_a_run_headed_past_its_budget_with_its_layers_trips_before_the_layers(tmp_path):
    log = tmp_path / "snappyHexMesh.log"
    log.write_text("")
    brake = SB.SizeBrake(log, 2_000_000)
    assert brake() is None
    log.write_text(A2_LOG.split("Ideal")[0])        # castellated and snapped: within
    assert brake() is None
    log.write_text(A2_LOG)                          # the ideal-with-layers line arrives
    reason = brake()
    assert reason and "with layers" in reason
    assert brake.tripped == {"stage": "with layers", "cells": 13_076_800, "budget": 2_000_000,
                             "margin": 2.0, "ratio": 6.54}


def test_a_castellated_mesh_far_past_its_budget_trips_at_once(tmp_path):
    log = tmp_path / "snappyHexMesh.log"
    log.write_text("Refined mesh : cells:3244912  faces:1  points:1  unbalance:0.01\n")
    brake = SB.SizeBrake(log, 2_000_000)
    assert brake() and brake.tripped["stage"] == "castellated" and brake.tripped["ratio"] == 1.62


def test_a_run_within_its_plan_is_never_stopped(tmp_path):
    log = tmp_path / "snappyHexMesh.log"
    log.write_text(WITHIN_LOG)
    brake = SB.SizeBrake(log, 2_000_000)
    assert brake() is None and brake.tripped is None


def test_the_brake_stops_a_real_process_through_the_watch_hook(tmp_path):
    log = tmp_path / "snappyHexMesh.log"
    brake = SB.SizeBrake(log, 2_000_000)
    code = ("import time\nprint('Refined mesh : cells:2001224  faces:1', flush=True)\n"
            "time.sleep(0.3)\nprint('Ideal layer addition : cells:1.30768e+07  unbalance:0.007', flush=True)\n"
            "time.sleep(30)")
    with pytest.raises(StoppedByWatch) as err, log.open("w") as fh:
        run_guarded([sys.executable, "-u", "-c", code], stdout=fh, stderr=subprocess.STDOUT, timeout=60,
                    stall_s=30, poll_s=0.1, watch=brake)
    assert "6.5 times its budget" in err.value.reason and brake.tripped["stage"] == "with layers"


def test_the_next_pass_is_cut_by_how_far_over_it_was_headed():
    tripped = {"stage": "with layers", "cells": 13_076_800, "budget": 2_000_000}
    factor = SB.scaled_budget_factor(tripped)
    assert factor == SB.SIZE_BRAKE_STEP == 0.5          # never deeper than half in one pass
    plan = {"max_cells": 2_000_000}
    out, changes = coarsen_after_timeout({**plan, "_why": "oversize", "_budget_factor": factor},
                                         dict(plan), ceiling=8_000_000, internal=False)
    assert out["max_cells"] == int(factor * 2_000_000) and changes
    # a stall or a wall still halves it, as before
    out, _ = coarsen_after_timeout(dict(plan), dict(plan), ceiling=8_000_000, internal=False)
    assert out["max_cells"] == 1_000_000


def test_the_case_budget_is_read_from_the_case(tmp_path):
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "snappyHexMeshDict").write_text("castellatedMeshControls { maxLocalCells 2000000; "
                                                           "maxGlobalCells 2000000; }")
    assert SB.case_budget(tmp_path) == 2_000_000
    assert SB.case_budget(tmp_path / "missing") is None


# THE PLAN COUNTS THE LAYERS

def test_a_large_thin_plate_is_planned_with_room_for_its_layers():
    # A2-like: the castellation would want far more than the budget; most of it touches the wall
    final = final_estimate(castellated=9.0e6, budget=2_000_000, area=4.0, wall_cell=0.0015,
                           n_layers=5)
    assert final > 1.5 * 2_000_000
    cast = castellation_budget(2_000_000, final, margin=1.5)
    assert 200_000 <= cast < 2_000_000
    assert final_estimate(castellated=9.0e6, budget=cast, area=4.0, wall_cell=0.0015,
                          n_layers=5) <= 1.5 * 2_000_000 * 1.01


def test_a_bulky_body_within_its_budget_keeps_its_plan_unchanged():
    # bluff-body-like: a third more with its layers, inside the margin - the dict is the old one
    final = final_estimate(castellated=450_000, budget=2_000_000, area=0.5, wall_cell=0.004,
                           n_layers=5)
    assert final < 1.5 * 2_000_000
    assert castellation_budget(2_000_000, final, margin=1.5) == 2_000_000
    assert castellation_budget(2_000_000, None, margin=1.5) == 2_000_000



def test_the_brake_measures_against_the_plans_budget_not_the_castellation_budget(tmp_path):
    """A plate's layers are about 3.5x its castellated cells at any size: measured against a
    castellation budget cut to leave room for them, the brake tripped on every pass and shrank the
    mesh until the body was lost (lab, 25 Oct). It reads the plan's budget, beside the case."""
    import json

    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "snappyHexMeshDict").write_text("maxGlobalCells 571428;")
    (tmp_path / SB.PLAN_CELLS_FILE).write_text(json.dumps({"plan_cells": 2_000_000}))
    assert SB.case_budget(tmp_path) == 2_000_000
    log = tmp_path / "snappyHexMesh.log"
    log.write_text("Refined mesh : cells:571000  faces:1\nIdeal layer addition : cells:2.0e+06\n")
    assert SB.SizeBrake(log, SB.case_budget(tmp_path))() is None


def test_the_castellation_budget_is_never_cut_under_a_quarter_of_the_plan():
    from meshpipeline.engines.snappy.snappy_runner import CASTELLATION_FLOOR_SHARE
    assert CASTELLATION_FLOOR_SHARE == 0.25
    assert castellation_budget(2_000_000, 40_000_000, margin=1.5) == 500_000


# NEVER BELOW THE CASE'S OWN BACKGROUND MESH (release Gate C, 26 Oct)

_GATE_C_LOG = """Refined mesh : cells:131112  faces:400000  points:140000  unbalance:0.02
Ideal layer addition : cells:135816  unbalance:0.01
Layer mesh : cells:135816  faces:416900  points:145748  unbalance:0.0099
"""


def _case(tmp_path, *, plan: int, divisions=(60, 48, 48)):
    import json
    (tmp_path / "system").mkdir(parents=True, exist_ok=True)
    (tmp_path / "system" / "blockMeshDict").write_text(
        "vertices ( (0 0 0) (1 0 0) (1 1 0) (0 1 0) (0 0 1) (1 0 1) (1 1 1) (0 1 1) );\n"
        f"blocks (hex (0 1 2 3 4 5 6 7) ({divisions[0]} {divisions[1]} {divisions[2]}) simpleGrading (1 1 1)); edges ();\n")
    (tmp_path / SB.PLAN_CELLS_FILE).write_text(json.dumps({"plan_cells": plan}))
    return tmp_path


def test_the_background_mesh_is_read_from_the_cases_blockmesh(tmp_path):
    assert SB.background_cells(_case(tmp_path, plan=40_000)) == 60 * 48 * 48 == 138_240
    assert SB.background_cells(tmp_path / "nowhere") == 0


def test_a_plan_under_its_own_background_is_measured_against_the_background(tmp_path):
    """The native snappy pass of release Gate C: a 40,000-cell plan on a 138,240-cell blockMesh. No
    castellation budget can shrink the background, so measured against 40,000 the brake read 3.3x and
    stopped a sound mesh - but only on 8 oversubscribed ranks, slow enough for the watch to look."""
    ws = _case(tmp_path, plan=40_000)
    assert SB.case_budget(ws) == 138_240
    log = tmp_path / "snappyHexMesh.log"
    log.write_text(_GATE_C_LOG)
    brake = SB.SizeBrake(log, SB.case_budget(ws))
    assert brake() is None and brake.tripped is None


def test_a_plan_above_its_background_is_measured_against_the_plan(tmp_path):
    ws = _case(tmp_path, plan=2_000_000, divisions=(40, 30, 30))
    assert SB.case_budget(ws) == 2_000_000


def test_refined_far_past_its_background_still_trips(tmp_path):
    ws = _case(tmp_path, plan=40_000)
    log = tmp_path / "snappyHexMesh.log"
    log.write_text("Refined mesh : cells:900000  faces:1  points:1  unbalance:0.01\n")
    brake = SB.SizeBrake(log, SB.case_budget(ws))
    assert brake() and brake.tripped["budget"] == 138_240
