# Responsibility: Verify the cell record says where a snappyHexMesh mesh's cells went - by octree level, by the
# refinement that asked for it and the rule behind it (engines/snappy/cell_record, rulebook 75).
# Boundaries: the plan the internal case writes and the mesher's own log text; no mesher runs.
from __future__ import annotations

from pathlib import Path

from meshpipeline.engines.snappy import cell_record as CR
from meshpipeline.engines.snappy import snappy_runner as R

#: the shape of snappyHexMesh's own log: tables during castellation, the one after "Refined mesh"
#: (the mesh cut to the fluid), the snapped count, the layered count and a last table
LOG = """Cells per refinement level:
    0	900
    1	100
After refinement surface refinement iteration 0 : cells:1000
Refined mesh : cells:61000  faces:1  points:1
Cells per refinement level:
    0	1000
    1	4000
    2	16000
    3	40000
Writing mesh to time constant
Snapped mesh : cells:61000  faces:1  points:1
Layer mesh : cells:91000  faces:1  points:1
Cells per refinement level:
    0	1000
    1	4000
    2	16000
    3	70000
"""


def test_the_log_gives_the_castellated_levels_and_the_layer_cells():
    got = CR.levels_from_log(LOG)
    assert got["castellated"] == {0: 1000, 1: 4000, 2: 16000, 3: 40000}
    assert got["refined"] == 61000 and got["layer_cells"] == 30000
    assert CR.levels_from_log("no mesh was built") is None


def test_each_level_goes_to_the_refinements_that_asked_for_it():
    plan = {"base_cell_m": 0.008, "entries": [
        CR.entry("wall", 2, "cells across the bore"), CR.entry("near-wall band", 3, "band"),
        CR.entry("narrow passages", 3, "PLAN-cfd-1"), CR.entry("opening inlet", 2, "PLAN-cfd-2")]}
    rec = CR.record(plan, CR.levels_from_log(LOG))
    rows = {r["purpose"]: r for r in rec["rows"]}
    assert rec["cells"] == 91000
    assert rows["near-wall band + narrow passages"]["cells"] == 40000
    assert rows["near-wall band + narrow passages"]["rules"] == ["band", "PLAN-cfd-1"]
    assert rows["wall + opening inlet"]["cells"] == 16000
    assert rows["wall layers"]["cells"] == 30000 and rows["wall layers"]["share"] == round(30000 / 91000, 4)
    # below the lowest level asked for is the background; a level between asked ones is grading
    assert rows["core"]["cells"] == 5000 and rows["core"]["levels"] == [0, 1]
    assert rows["wall + opening inlet"]["cell_m"] == [0.002]
    assert CR.words(rec).startswith("0.091 M cells: near-wall band + narrow passages 44%")


def test_a_level_nobody_asked_for_between_two_asked_ones_is_grading():
    plan = {"base_cell_m": 1.0, "entries": [CR.entry("wall", 1, "x"), CR.entry("ports", 3, "y")]}
    rec = CR.record(plan, {"castellated": {0: 10, 1: 20, 2: 30, 3: 40}})
    rows = {r["purpose"]: r["cells"] for r in rec["rows"]}
    assert rows == {"core": 10, "wall": 20, "grading": 30, "ports": 40}


def test_the_internal_case_writes_its_plan_and_finalize_reads_the_record(tmp_path):
    ws = tmp_path
    (ws / "system").mkdir()
    R.render_internal_case(
        ws, names={"wall": "wall", "inlet": "inlet", "outlet": "outlet"},
        features={"wall": "wall.eMesh", "inlet": "inlet.eMesh", "outlet": "outlet.eMesh"},
        interior_point=[0.5, 0.0, 0.0], bbox_min=[0.0, -0.05, -0.05], bbox_max=[1.0, 0.05, 0.05],
        base_cell=0.1 / 24 * 4, surface_level=2, feature_level=3, n_layers=5,
        port_sizes={"inlet": 0.1, "outlet": 0.004})
    plan = CR.read_plan(ws)
    purposes = {e["purpose"]: e for e in plan["entries"]}
    assert {"wall", "seed bubble", "feature edges", "near-wall band", "opening inlet",
            "opening outlet"} <= set(purposes)
    assert purposes["opening outlet"]["rule"] == "PLAN-cfd-2"
    assert purposes["opening outlet"]["level"] > purposes["opening inlet"]["level"], \
        "the small opening asked for its own finer level"
    assert CR.of_workspace(ws) == {}, "no log yet: no record"
    Path(ws / "snappyHexMesh.log").write_text(LOG)
    assert CR.of_workspace(ws)[CR.QUALITY_KEY]["cells"] == 91000
