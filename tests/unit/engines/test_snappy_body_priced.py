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


# RUN-8: A STOPPED PASS TEACHES THE RE-PLAN WHAT ITS WALL HOLDS

def _render_k(ws: Path, budget: int, *, k: float | None = None, requirements=None) -> dict:
    (ws / "system").mkdir(parents=True, exist_ok=True)
    strategy = {"n_layers": 5, "last_layer_rel": 0.35, "max_cells": budget}
    if k is not None:
        strategy["_wall_face_factor"] = k
    return R.render_snappy_case(ws, surface_name="body", feature_file="body.eMesh", analysis=ANALYSIS,
                                recommendation=REC, domain_min=[-4.0, -3.0, -3.0], domain_max=[7.0, 4.0, 4.0],
                                strategy=strategy, requirements=requirements)


def _priced_budget_edge(tmp_path) -> int:
    """A budget the plan's own estimate fits (no pricing) - just above the floor's need / 1.5."""
    _render_k(tmp_path / "probe", 50_000)
    need = json.loads((tmp_path / "probe" / PLAN_CELLS_FILE).read_text())["body_needs"]
    return int(need / 1.4)


def test_the_plan_records_the_wall_faces_it_priced(tmp_path):
    _render_k(tmp_path, 8_000_000)
    rec = json.loads((tmp_path / PLAN_CELLS_FILE).read_text())
    assert rec["layers_est"] == 5 and rec["wall_faces_est"] > 0
    assert "wall_face_factor" not in rec


def test_a_learned_wall_prices_the_body_the_estimate_had_fitted(tmp_path):
    budget = _priced_budget_edge(tmp_path)
    out0 = _render_k(tmp_path / "a", budget)
    assert "size_note" not in out0, "by its own estimate the plan fits"
    out = _render_k(tmp_path / "b", budget, k=4.0)
    rec = json.loads((tmp_path / "b" / PLAN_CELLS_FILE).read_text())
    assert "comes to about" in out["size_note"] and rec["wall_face_factor"] == 4.0
    assert rec["body_needs"] > 1.5 * budget and rec["plan_cells"] >= rec["body_needs"]


def test_requirements_without_a_count_do_not_turn_the_pricing_off(tmp_path):
    # RM5 ran in "OK" mode: its notes asked a wall treatment, no count - the pricing must still hold
    from meshpipeline.engines import cfd_requirements as CR
    ok = CR.Ask({"layers": {"treatment": "functions"}}, "snappy")
    assert not ok.user_budget()
    out = _render_k(tmp_path / "ok", 50_000, requirements=ok)
    assert "comes to about" in out.get("size_note", "")
    theirs = CR.Ask({"cells": {"target": 50_000}}, "snappy")
    out = _render_k(tmp_path / "count", 50_000, requirements=theirs)
    assert "comes to about" not in out.get("size_note", ""), "a count the user set stands"
