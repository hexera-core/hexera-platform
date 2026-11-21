# Responsibility: Verify a body at the coarsest wall that seals it is planned at what it needs, layers included, and
# the size brake measures the run against that need - never a budget no re-plan can reach (rulebook 75).
# Boundaries: the dictionary and the plan record render_snappy_case writes; no OpenFOAM run.
from __future__ import annotations

import json
import re
from pathlib import Path

from meshpipeline.engines.size_brake import PLAN_CELLS_FILE
from meshpipeline.engines.snappy import snappy_runner as R

REC = {"base_cell": 0.041666666666666664, "surface_level": (2, 2), "afford_level": 3,
       "feature_level": 3, "feature_level_true": 3, "budget_capped": False,
       "distance_bands": [(0.0625, 2), (0.25, 1)], "resolve_feature_angle": 35.0,
       "min_feature": 0.001, "surface_area": 6.0, "cells_across_min_feature": 8}
ANALYSIS = {"bbox_min": [0.0, -0.5, -0.5], "bbox_max": [1.0, 0.5, 0.5], "L": 1.0, "surface_area": 6.0}


def _render(ws: Path, budget: int) -> dict:
    (ws / "system").mkdir(parents=True, exist_ok=True)
    return R.render_snappy_case(ws, surface_name="body", feature_file="body.eMesh", analysis=ANALYSIS,
                                recommendation=REC, domain_min=[-4.0, -3.0, -3.0], domain_max=[7.0, 4.0, 4.0],
                                strategy={"n_layers": 5, "last_layer_rel": 0.35, "max_cells": budget})


def _max_global(ws: Path) -> int:
    return int(re.search(r"maxGlobalCells (\d+);", (ws / "system" / "snappyHexMeshDict").read_text()).group(1))


def test_a_body_whose_floor_mesh_outgrows_its_budget_is_planned_at_what_it_needs(tmp_path):
    out = _render(tmp_path, budget=50_000)
    rec = json.loads((tmp_path / PLAN_CELLS_FILE).read_text())
    assert rec["body_needs"] > 1.5 * 50_000
    assert rec["plan_cells"] >= rec["body_needs"], "the brake measures the run against its own need"
    assert "comes to about" in out["size_note"] and "more than the 0.05 M planned: it is meshed at that" in out["size_note"]
    assert _max_global(tmp_path) > 50_000, "snappy's ceiling is not cut short of the body's own mesh"


def test_a_body_inside_its_budget_is_written_as_it_always_was(tmp_path):
    out = _render(tmp_path, budget=8_000_000)
    rec = json.loads((tmp_path / PLAN_CELLS_FILE).read_text())
    assert "size_note" not in out and "body_needs" not in rec
    assert rec["plan_cells"] == 8_000_000
