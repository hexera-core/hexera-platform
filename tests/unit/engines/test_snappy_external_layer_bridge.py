# Responsibility: Verify a y+ 1 first layer round a BODY gets the layers that carry it out to the wall cells, as the
# internal case's always did (CFDREQ-9's bridge at the external renderer, rulebook 75).
# Boundaries: the dictionary render_snappy_case writes for a small staged body; no OpenFOAM run.
from __future__ import annotations

import json
import re
from pathlib import Path

from meshpipeline.cad.stl_io import write_stl_binary
from meshpipeline.engines import cfd_requirements as CR
from meshpipeline.engines import mesh_requirements as MR
from meshpipeline.engines.snappy import snappy_runner as R

REC = {"base_cell": 0.041666666666666664, "surface_level": (2, 2), "afford_level": 3,
       "feature_level": 3, "feature_level_true": 3, "budget_capped": False,
       "distance_bands": [(0.0625, 2), (0.25, 1)], "resolve_feature_angle": 35.0,
       "min_feature": 0.001, "surface_area": 1.0, "cells_across_min_feature": 8}
ANALYSIS = {"bbox_min": [0.0, -0.5, -0.5], "bbox_max": [1.0, 0.5, 0.5], "L": 1.0}


def _box_stl(path: Path) -> None:
    import numpy as np
    v = np.array([[0, -.5, -.5], [1, -.5, -.5], [1, .5, -.5], [0, .5, -.5],
                  [0, -.5, .5], [1, -.5, .5], [1, .5, .5], [0, .5, .5]], dtype=float)
    f = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
         (2, 3, 7), (2, 7, 6), (1, 2, 6), (1, 6, 5), (0, 4, 7), (0, 7, 3)]
    path.parent.mkdir(parents=True, exist_ok=True)
    write_stl_binary(path, v[np.asarray(f)])


def _ask(first_mm: float) -> CR.Ask:
    fields = {"stated_unit": "none", "sizes": [], "counts": [], "local": [], "physics": [],
              "element_kind": [], "limits": [], "other": [],
              "layers": [{"what": "first_height", "value": first_mm, "unit": "mm",
                          "quote": f"first layer {first_mm:g} mm"}]}
    req, _d, _na = MR.check(json.dumps(fields), f"first layer {first_mm:g} mm", route="flow")
    return CR.Ask(req, "snappy")


def _layers(ws: Path) -> int:
    text = (ws / "system" / "snappyHexMeshDict").read_text()
    return int(re.search(r"nSurfaceLayers\s+(\d+)\s*;", text).group(1))


def test_a_resolved_first_layer_round_a_body_gets_the_layers_that_reach_its_wall_cells(tmp_path):
    ws = tmp_path
    (ws / "system").mkdir()
    _box_stl(ws / "constant" / "triSurface" / "body.stl")
    R.render_snappy_case(ws, surface_name="body", feature_file="body.eMesh", analysis=ANALYSIS,
                         recommendation=REC, domain_min=[-4.0, -3.0, -3.0], domain_max=[7.0, 4.0, 4.0],
                         strategy={"n_layers": 5, "last_layer_rel": 0.35, "max_cells": 2_000_000},
                         requirements=_ask(0.034))
    # 0.034 mm under ~10 mm wall cells: far more than the plan's 5 layers carry it out to the cells
    assert _layers(ws) > 5
    text = (ws / "system" / "snappyHexMeshDict").read_text()
    # CFDREQ-33 amended (rulebook 113): a box at one wall level - the first attempt writes the stack in metres, marked
    assert "firstLayerThickness" in text and "relativeSizes false" in text and "relativeOnRetry" in text
    assert "plainWallCell" not in text
    # ...and the attempt after its layer-stack refusal (the carried gate facts) writes it relative to the plain cell
    from meshpipeline.engines.gate_facts import write_gate_facts
    write_gate_facts(ws, "layer_stack", {"coverage_pct": 61.8, "relative_next": True})
    R.render_snappy_case(ws, surface_name="body", feature_file="body.eMesh", analysis=ANALYSIS,
                         recommendation=REC, domain_min=[-4.0, -3.0, -3.0], domain_max=[7.0, 4.0, 4.0],
                         strategy={"n_layers": 5, "last_layer_rel": 0.35, "max_cells": 2_000_000},
                         requirements=_ask(0.034))
    text = (ws / "system" / "snappyHexMeshDict").read_text()
    assert "relativeSizes true" in text and "plainWallCell" in text and "relativeOnRetry" not in text


def test_a_first_layer_the_wall_cells_keep_is_left_at_the_plan_s_count(tmp_path):
    ws = tmp_path
    (ws / "system").mkdir()
    _box_stl(ws / "constant" / "triSurface" / "body.stl")
    R.render_snappy_case(ws, surface_name="body", feature_file="body.eMesh", analysis=ANALYSIS,
                         recommendation=REC, domain_min=[-4.0, -3.0, -3.0], domain_max=[7.0, 4.0, 4.0],
                         strategy={"n_layers": 5, "last_layer_rel": 0.35, "max_cells": 2_000_000},
                         requirements=_ask(1.0))
    assert _layers(ws) <= 5
