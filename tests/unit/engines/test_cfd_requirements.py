# Responsibility: Verify the user's requirements on a flow (CFD) mesh (NOTE-10, CFDREQ-1..7): the flow fields of the one reading (a cell count, boundary layers, sizes in a place, a size at the walls, the flow's quality measures and physics), a y+ as a first layer height, each engine's levers (snappyHexMesh external and internal, cfMesh, the multi-region mesher, VMTK, gmsh flow, the snap grid) - applied, the nearest, or not applied and told - the per-cell measures, asked against delivered on the result and in the verdict, and nothing said = byte-identical cases.
# Boundaries: pure over plain data, temporary workspaces and the dict writers; no model, no mesher is run.
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from meshpipeline.engines import cfd_cell_measures as CM
from meshpipeline.engines import cfd_requirements as CR
from meshpipeline.engines import mesh_requirements as MR


def _answer(**fields) -> str:
    base = {"stated_unit": "none", "sizes": [], "counts": [], "layers": [], "local": [], "physics": [],
            "element_kind": [], "limits": [], "other": []}
    base.update(fields)
    return json.dumps(base)


def _read(notes: str, **fields):
    return MR.check(_answer(**fields), notes, route="flow")


def _lim(measure, target, quote, direction="max", pct=0, outer=0, has_outer=False):
    return {"measure": measure, "shape": "any", "direction": direction, "target": target, "allowed_percent": pct,
            "outer": outer, "has_outer": has_outer, "quote": quote}


NOTES = ("Base cell 20 mm, surface cells 2 mm, min cell size 0.5 mm.\n5 prism layers, first layer 0.02 mm, growth 1.2.\n"
         "y+ around 1. Max 8 million cells. Non-ortho under 65, skewness below 4.\nRefine around the wing to 2 mm.\n"
         "Air at 30 m/s, chord 0.5 m.")


def _full():
    return _read(NOTES,
                 sizes=[{"what": "global", "value": 20, "unit": "mm", "quote": "Base cell 20 mm"},
                        {"what": "near_wall", "value": 2, "unit": "mm", "quote": "surface cells 2 mm"},
                        {"what": "min_edge", "value": 0.5, "unit": "mm", "quote": "min cell size 0.5 mm"}],
                 counts=[{"what": "ceiling", "value": 8000000, "quote": "Max 8 million cells"}],
                 layers=[{"what": "count", "value": 5, "unit": "none", "quote": "5 prism layers"},
                         {"what": "first_height", "value": 0.02, "unit": "mm", "quote": "first layer 0.02 mm"},
                         {"what": "growth", "value": 1.2, "unit": "none", "quote": "growth 1.2"},
                         {"what": "y_plus", "value": 1, "unit": "none", "quote": "y+ around 1"}],
                 local=[{"where": "the wing", "what": "size", "value": 2, "unit": "mm",
                         "quote": "Refine around the wing to 2 mm"}],
                 physics=[{"what": "air", "value": 0, "unit": "none", "quote": "Air at 30 m/s"},
                          {"what": "velocity", "value": 30, "unit": "m/s", "quote": "Air at 30 m/s"},
                          {"what": "length", "value": 0.5, "unit": "m", "quote": "chord 0.5 m"}],
                 limits=[_lim("non_orthogonality", 65, "Non-ortho under 65"), _lim("skewness", 4, "skewness below 4")])


# ------------------------------------------------------------------ NOTE-10: the reading -----------------------
def test_the_flow_fields_are_read_checked_and_said_in_cells():
    req, dropped, na = _full()
    assert dropped == [] and na == []
    assert req["sizes"]["global_mm"] == 20 and req["sizes"]["near_wall_mm"] == 2 and req["sizes"]["min_edge_mm"] == 0.5
    assert req["cells"] == {"target": None, "ceiling": 8_000_000.0, "floor": None}
    assert req["layers"] == {"count": 5, "first_mm": 0.02, "total_mm": None, "growth": 1.2, "y_plus": 1.0}
    assert req["local"][0]["where"] == "the wing" and req["local"][0]["mm"] == 2
    assert req["physics"]["velocity_ms"] == 30 and req["physics"]["length_m"] == 0.5 and req["physics"]["fluid"] == "air"
    asked = [r["asked"] for r in req["rows"]]
    assert asked[:3] == ["cell size 20 mm", "cells of 2 mm at the walls", "no cell smaller than 0.5 mm"]
    assert "at most 8 million cells" in asked and "5 boundary layers" in asked
    assert "first boundary layer 0.02 mm thick" in asked and "y+ of about 1 at the walls" in asked
    assert "cells of 2 mm at the wing" in asked
    assert all(narration_ok(line) for line in MR.live_words(MR.section("used", requirements=req)))


def narration_ok(line: str) -> bool:
    from meshpipeline.contracts import narration
    return bool(narration.plain(line))


@pytest.mark.parametrize("quote,value", [("max 8 million cells", 8e6), ("under 2M cells", 2e6), ("500k cells", 5e5),
                                         ("8,000,000 cells at most", 8e6), ("no more than 2.5M", 2.5e6),
                                         ("1e6 cells", 1e6)])
def test_a_count_is_read_with_its_multiplier(quote, value):
    req, dropped, _ = _read(quote, counts=[{"what": "ceiling", "value": value, "quote": quote}])
    assert dropped == [] and req["cells"]["ceiling"] == value


def test_a_count_not_in_the_words_and_a_floor_over_the_ceiling_are_told():
    _req, dropped, _ = _read("max 8 million cells", counts=[{"what": "ceiling", "value": 9e6,
                                                              "quote": "max 8 million cells"}])
    assert dropped and "not in those words" in dropped[0]
    req, dropped, _ = _read("at least 5M cells, at most 2M cells",
                            counts=[{"what": "floor", "value": 5e6, "quote": "at least 5M cells"},
                                    {"what": "ceiling", "value": 2e6, "quote": "at most 2M cells"}])
    assert req["cells"]["floor"] is None and req["cells"]["ceiling"] is None and "neither" in dropped[0]


def test_layers_in_metres_scientific_and_no_layers():
    req, dropped, _ = _read("first cell height 1e-5 m", layers=[{"what": "first_height", "value": 1e-5, "unit": "m",
                                                                  "quote": "first cell height 1e-5 m"}])
    assert dropped == [] and math.isclose(req["layers"]["first_mm"], 0.01)
    req, dropped, _ = _read("no boundary layers please", layers=[{"what": "count", "value": 0, "unit": "none",
                                                                   "quote": "no boundary layers please"}])
    assert req["layers"]["count"] == 0 and [r["asked"] for r in req["rows"]] == ["no boundary layers"]
    req, dropped, _ = _read("first layer 2 mm, total 1 mm",
                            layers=[{"what": "first_height", "value": 2, "unit": "mm", "quote": "first layer 2 mm"},
                                    {"what": "total_thickness", "value": 1, "unit": "mm", "quote": "total 1 mm"}])
    assert req["layers"]["total_mm"] is None and "not thinner" in dropped[0]


def test_the_cfd_measures_are_limits():
    req, dropped, _ = _read("min determinant 0.001, orthogonal quality above 0.1, min volume 1e-15",
                            limits=[_lim("determinant", 0.001, "min determinant 0.001", "min"),
                                    _lim("orthogonal_quality", 0.1, "orthogonal quality above 0.1", "min"),
                                    _lim("min_volume", 1e-15, "min volume 1e-15", "min")])
    assert dropped == [] and [(x["measure"], x["op"]) for x in req["limits"]] == [
        ("determinant", ">="), ("orthogonal_quality", ">="), ("min_volume", ">=")]


def test_the_flow_fields_on_the_solid_route_are_told_in_elements_and_not_applied_there():
    from meshpipeline.engines import fea_requirements as FR

    req, _d, _na = MR.check(_answer(layers=[{"what": "count", "value": 5, "unit": "none", "quote": "5 prism layers"}],
                                    counts=[{"what": "ceiling", "value": 5e5, "quote": "max 500k elements"}]),
                            "5 prism layers, max 500k elements", route="fea")
    assert [r["asked"] for r in req["rows"]] == ["at most 500,000 elements", "5 boundary layers"]
    rows = {r["field"]: r for r in [{"id": r["id"], "field": r["field"], "asked": r["asked"],
                                     **MR.elements_row(r["field"], req, 400_000, "solid")} for r in req["rows"]]}
    assert rows["cells:ceiling"]["met"] is True and rows["layers:count"]["applied"] is False
    assert FR.summary is not None


# ------------------------------------------------------------------ CFDREQ-4: y+ ---------------------------------
def test_a_y_plus_is_a_first_layer_height_from_the_flows_numbers():
    h, missing, how = CR.y_plus_height(1.0, {"velocity_ms": 30.0, "fluid": "air", "length_m": 0.5})
    re_ = 30.0 * 0.5 / CR.NU["air"]
    cf = 0.026 * re_ ** (-1 / 7)
    assert missing == [] and math.isclose(h, CR.NU["air"] / (30.0 * math.sqrt(cf / 2)), rel_tol=1e-9)
    assert 5e-6 < h < 2e-5 and how["reynolds"] == float(f"{re_:.4g}")
    # Reynolds and a length are enough
    h2, missing, _ = CR.y_plus_height(1.0, {"reynolds": re_}, length_m=0.5)
    assert missing == [] and math.isclose(h, h2, rel_tol=1e-9)
    # the missing number is named; nothing new is asked
    h3, missing, _ = CR.y_plus_height(1.0, {"fluid": "air"}, length_m=1.0)
    assert h3 is None and missing == ["the flow speed"]
    _h, missing, _ = CR.y_plus_height(1.0, {})
    assert missing == ["the flow speed", "the fluid (air or water) or its viscosity", "a reference length"]


def test_a_y_plus_without_the_speed_keeps_our_layers_and_says_why(tmp_path):
    req, _d, _na = _read("y+ around 1", layers=[{"what": "y_plus", "value": 1, "unit": "none", "quote": "y+ around 1"}])
    ask = CR.Ask(req, "snappy")
    assert ask.first_layer(length_m=1.0) is None
    row = ask.rows[ask.rid("layers:y_plus")]
    assert row["status"] == "not_applied" and "the flow speed and the fluid" in row["built"]


# ------------------------------------------------------------------ CFDREQ-3: the budget -------------------------
def test_the_users_count_beats_ours_up_to_the_largest_machine():
    req, _d, _ = _read("max 2M cells", counts=[{"what": "ceiling", "value": 2e6, "quote": "max 2M cells"}])
    ask = CR.Ask(req, "snappy")
    assert ask.budget(6_000_000, hard=8_000_000) == 2_000_000
    req, _d, _ = _read("max 20M cells", counts=[{"what": "ceiling", "value": 2e7, "quote": "max 20M cells"}])
    ask = CR.Ask(req, "snappy")
    assert ask.budget(6_000_000, hard=8_000_000) == 8_000_000
    assert ask.rows[ask.rid("cells:ceiling")]["status"] == "nearest"
    req, _d, _ = _read("cell size 5 mm", sizes=[{"what": "global", "value": 5, "unit": "mm", "quote": "cell size 5 mm"}])
    assert CR.Ask(req, "snappy").budget(6_000_000, hard=8_000_000) == 6_000_000      # no count: ours, unchanged


# ------------------------------------------------------------------ snappyHexMesh ---------------------------------
_ANALYSIS = {"bbox_min": [0.0, -0.3, -0.3], "bbox_max": [0.1, 0.3, 0.3], "L": 0.1, "extent": [0.1, 0.6, 0.6],
             "surface_area": 0.5}
_REC = {"base_cell": 0.02, "surface_level": [3, 3], "feature_level": 4, "afford_level": 5,
        "distance_bands": [(0.04, 3), (0.12, 2)], "resolve_feature_angle": 30.0}


def _snappy(ws: Path, ask=None) -> tuple[str, dict]:
    (ws / "system").mkdir(parents=True, exist_ok=True)
    from meshpipeline.engines.snappy import snappy_runner as R
    out = R.render_snappy_case(ws, surface_name="body", feature_file="body.eMesh", analysis=_ANALYSIS,
                               recommendation=dict(_REC), domain_min=[-1.0, -2.0, -2.0], domain_max=[2.0, 2.0, 2.0],
                               strategy={"n_layers": 3, "max_cells": 2_000_000}, requirements=ask)
    return (ws / "system" / "snappyHexMeshDict").read_text(), out


def test_snappy_nothing_asked_writes_the_same_case(tmp_path):
    a, sa = _snappy(tmp_path / "a")
    b, sb = _snappy(tmp_path / "b", None)
    assert a == b and sa == sb and not (tmp_path / "a" / CR.PLAN_FILE).exists()
    assert (tmp_path / "a" / "system" / "blockMeshDict").read_text() == (tmp_path / "b" / "system" /
                                                                          "blockMeshDict").read_text()


def test_snappy_takes_the_wall_size_layers_growth_and_limits(tmp_path):
    req, _d, _ = _full()
    req["physics"] = {**req["physics"]}
    ask = CR.Ask(req, "snappy")
    text, summary = _snappy(tmp_path / "c", ask)
    assert "relativeSizes false;" in text and "firstLayerThickness 2e-05;" in text and "expansionRatio 1.2;" in text
    assert "finalLayerThickness" not in text.split("addLayersControls")[1].split("meshQualityControls")[0]
    assert re_num(text, "maxNonOrtho") == 65 and re_num(text, "maxInternalSkewness") == 4
    assert "nCellsBetweenLevels" in text
    plan = json.loads((tmp_path / "c" / CR.PLAN_FILE).read_text())["rows"]
    asked = {r["field"]: r["id"] for r in req["rows"]}
    assert plan[asked["near_wall"]]["status"] in ("applied", "nearest")
    assert plan[asked["layers:y_plus"]]["status"] == "applied"           # the first height is used as given
    assert summary["requirements_lines"]
    # the wall level holds 2 mm on the background really used
    base = summary["divisions"] and max((3.0 / summary["divisions"][0]), 4.0 / summary["divisions"][1])
    assert summary["surface_level"][1] == CR.level_for(base, 0.002, lo=0, hi=10) or plan[asked["near_wall"]][
        "status"] == "nearest"


def re_num(text: str, key: str) -> float:
    import re
    return float(re.search(rf"\b{key}\s+([-+0-9.eE]+);", text).group(1))


def test_every_line_said_before_meshing_passes_the_live_guard(tmp_path):
    from meshpipeline.contracts import narration

    req, _d, _ = _full()
    asks = []
    ask = CR.Ask(req, "snappy")
    _snappy(tmp_path / "g", ask)
    asks.append(ask)
    for engine, fn in (("cfmesh", lambda a: CR.cfmesh_strategy(a, {"n_layers": 3}, length_m=1.0)),
                       ("vmtk", lambda a: CR.vmtk_spec(a, {"boundary_layers": 5, "max_cells": 8_000_000})),
                       ("gmsh", lambda a: CR.gmsh_sizes(a, 0.01, external=True)),
                       ("snapgrid", lambda a: CR.snapgrid_plan(a, {"max_cells": 4_000_000, "growth": 1.3}))):
        a = CR.Ask(req, engine)
        fn(a)
        a.write(tmp_path)
        asks.append(a)
    for a in asks:
        lines = CR.live([f"On this mesher, {x}" for x in a.lines()])
        assert lines and all(narration.plain(x) for x in lines), [x for x in lines if not narration.plain(x)]


def test_a_tight_non_orthogonality_with_layers_says_why_layers_may_be_taken_back(tmp_path):
    req, _d, _ = _read("5 layers, non-ortho under 60", layers=[{"what": "count", "value": 5, "unit": "none",
                                                                 "quote": "5 layers"}],
                       limits=[_lim("non_orthogonality", 60, "non-ortho under 60")])
    ask = CR.Ask(req, "snappy")
    ask.done("layers:count", "5 layers on every wall")
    text, _s = _snappy(tmp_path / "n", ask)
    assert re_num(text.split("relaxed")[1], "maxNonOrtho") == 58
    plan = json.loads((tmp_path / "n" / CR.PLAN_FILE).read_text())["rows"]
    assert "taken back" in plan[ask.rid("layers:count")]["note"]


def test_a_limit_between_our_snap_and_layer_bars_holds_the_layer_stage(tmp_path):
    # ours: 65 while snapping, 68 while adding layers (the finished mesh is held to 70). A user's 66 is tighter than
    # what the finished mesh is held to: the layer stage takes it (2 under), the snap keeps its own 65
    req, _d, _ = _read("non-ortho under 66", limits=[_lim("non_orthogonality", 66, "non-ortho under 66")])
    ask = CR.Ask(req, "snappy")
    text, _s = _snappy(tmp_path / "r", ask)
    head, relaxed = text.split("relaxed")
    assert re_num(head.split("meshQualityControls")[1], "maxNonOrtho") == 65 and re_num(relaxed, "maxNonOrtho") == 64
    assert ask.rows[req["limits"][0]["id"]]["status"] == "applied"


def test_a_share_within_its_outer_limit_is_met_whatever_the_worst_cell(tmp_path):
    lim = {"id": "r1", "measure": "non_orthogonality", "shape": "any", "op": "<=", "target": 65.0, "share": 0.01,
           "outer": 70.0, "quote": "x"}
    figs = {"non_orthogonality": np.array([10.0] * 999 + [67.0])}
    got = CR._limit_row(lim, {"max_non_ortho": 67.0}, figs, 1000, {})
    assert got["met"] is True and got["by"] == "every cell"
    got = CR._limit_row({**lim, "share": 0.0, "outer": None}, {"max_non_ortho": 67.0}, figs, 1000, {})
    assert got["met"] is False


def test_a_looser_limit_than_ours_keeps_ours_and_says_so(tmp_path):
    req, _d, _ = _read("non-ortho under 80", limits=[_lim("non_orthogonality", 80, "non-ortho under 80")])
    ask = CR.Ask(req, "snappy")
    text, _s = _snappy(tmp_path / "d", ask)
    assert re_num(text, "maxNonOrtho") == 65
    assert "already" in ask.rows[req["limits"][0]["id"]]["built"]


def test_snappy_growth_is_cells_between_levels():
    assert CR.cells_between_levels(1.2) == 4 and CR.cells_between_levels(1.26) == 3
    assert CR.cells_between_levels(2.0) == 1


def test_a_body_that_would_leak_keeps_its_sealing_level(tmp_path):
    req, _d, _ = _read("surface cells 50 mm", sizes=[{"what": "near_wall", "value": 50, "unit": "mm",
                                                       "quote": "surface cells 50 mm"}])
    ask = CR.Ask(req, "snappy")
    cap, lo, hi, _f = CR.snappy_levels(ask, 0.02, floor=3, level=3, flevel=4, regions=[], part_levels=False)
    assert lo == hi == 3 and ask.rows[ask.rid("near_wall")]["status"] == "nearest"


def test_a_place_not_in_the_model_is_told(tmp_path):
    req, _d, _ = _read("refine the spoiler to 1 mm", local=[{"where": "the spoiler", "what": "size", "value": 1,
                                                             "unit": "mm", "quote": "refine the spoiler to 1 mm"}])
    ask = CR.Ask(req, "snappy")
    CR.snappy_levels(ask, 0.02, floor=3, level=3, flevel=4, regions=["body", "wheels"], part_levels=False)
    assert ask.rows[req["local"][0]["id"]]["status"] == "not_applied"
    assert "spoiler" in ask.rows[req["local"][0]["id"]]["built"]


# ------------------------------------------------------------------ cfMesh -----------------------------------------
def _cfmesh(ws: Path, req=None) -> str:
    from meshpipeline.engines.cfmesh.cfmesh_runner import render_cfmesh_case
    ws.mkdir(parents=True, exist_ok=True)
    if req is not None:
        MR.write(ws, MR.section("used", requirements=req))
    render_cfmesh_case(ws, surface_file="geom.fms", wall_patch="body",
                       patches=[{"name": "body", "type": "wall"}, {"name": "farfield", "type": "patch"}],
                       body_bbox=([0, 0, 0], [1, 1, 1]), L=1.0, domain_min=[-5, -5, -5], domain_max=[5, 5, 5],
                       strategy={"n_layers": 3})
    return (ws / "system" / "meshDict").read_text()


def test_cfmesh_nothing_asked_writes_the_same_dict(tmp_path):
    assert _cfmesh(tmp_path / "a") == _cfmesh(tmp_path / "b")
    assert not (tmp_path / "a" / CR.PLAN_FILE).exists()


def test_cfmesh_takes_sizes_layers_and_a_wake(tmp_path):
    notes = "cell size 200 mm, wall cells 30 mm, 6 layers, layer growth 1.15, first layer 0.5 mm, refine the wake"
    req, dropped, _ = _read(notes,
                            sizes=[{"what": "global", "value": 200, "unit": "mm", "quote": "cell size 200 mm"},
                                   {"what": "near_wall", "value": 30, "unit": "mm", "quote": "wall cells 30 mm"}],
                            layers=[{"what": "count", "value": 6, "unit": "none", "quote": "6 layers"},
                                    {"what": "growth", "value": 1.15, "unit": "none", "quote": "layer growth 1.15"},
                                    {"what": "first_height", "value": 0.5, "unit": "mm",
                                     "quote": "first layer 0.5 mm"}],
                            local=[{"where": "the wake", "what": "finer", "value": 0, "unit": "none",
                                    "quote": "refine the wake"}])
    assert dropped == []
    text = _cfmesh(tmp_path / "c", req)
    # 200 mm halves to 25 mm, the nearest to 30 mm: built and said (the octree has no 30 mm under 200 mm)
    assert "maxCellSize 0.2;" in text and "body { cellSize 0.025025;" in text
    assert "nLayers 6; thicknessRatio 1.15; maxFirstLayerThickness 0.0005;" in text
    assert "notesWake" in text
    plan = json.loads((tmp_path / "c" / CR.PLAN_FILE).read_text())["rows"]
    assert all(p["status"] in ("applied", "nearest") for p in plan.values())
    wall = plan[next(r["id"] for r in req["rows"] if r["field"] == "near_wall")]
    assert wall["status"] == "nearest" and "25 mm" in wall["built"]
    # a wall size alone: the background is nudged so the walls land on it exactly
    req2, _d, _ = _read("wall cells 30 mm", sizes=[{"what": "near_wall", "value": 30, "unit": "mm",
                                                     "quote": "wall cells 30 mm"}])
    text2 = _cfmesh(tmp_path / "d", req2)
    assert "maxCellSize 0.24;" in text2 and "body { cellSize 0.03003;" in text2


# ------------------------------------------------------------------ VMTK, gmsh, the snap grid ----------------------
def test_vmtk_clamps_and_layers_and_what_it_cannot_do():
    req, _d, _ = _read("cells about 1 mm, 4 layers, first layer 0.01 mm",
                       sizes=[{"what": "global", "value": 1, "unit": "mm", "quote": "cells about 1 mm"}],
                       layers=[{"what": "count", "value": 4, "unit": "none", "quote": "4 layers"},
                               {"what": "first_height", "value": 0.01, "unit": "mm", "quote": "first layer 0.01 mm"}])
    ask = CR.Ask(req, "vmtk")
    s = CR.vmtk_spec(ask, {"boundary_layers": 5, "max_cells": 8_000_000})
    assert s["min_edge_length"] == pytest.approx(5e-4) and s["max_edge_length"] == pytest.approx(1.5e-3)
    assert s["boundary_layers"] == 4
    assert ask.rows[ask.rid("layers:first_height")]["status"] == "not_applied"


def test_gmsh_takes_the_size_and_tells_the_layers():
    req, _d, _ = _read("cells 3 mm, 5 layers", sizes=[{"what": "global", "value": 3, "unit": "mm", "quote": "cells 3 mm"}],
                       layers=[{"what": "count", "value": 5, "unit": "none", "quote": "5 layers"}])
    ask = CR.Ask(req, "gmsh")
    h, lo = CR.gmsh_sizes(ask, 0.01, external=False)
    assert h == pytest.approx(0.003) and lo is None
    assert ask.rows[ask.rid("layers:count")]["status"] == "not_applied"


def test_the_snap_grid_takes_its_background_growth_and_count():
    req, _d, _ = _read("background 5 mm, growth 1.2, max 3M cells",
                       sizes=[{"what": "global", "value": 5, "unit": "mm", "quote": "background 5 mm"},
                              {"what": "growth", "value": 1.2, "unit": "none", "quote": "growth 1.2"}],
                       counts=[{"what": "ceiling", "value": 3e6, "quote": "max 3M cells"}])
    plan = CR.snapgrid_plan(CR.Ask(req, "snapgrid"), {"max_cells": 4_000_000, "growth": 1.3})
    assert plan == {"max_cells": 3_000_000, "growth": 1.2, "background_size": 0.005}
    from meshpipeline.engines.snapgrid import runner as SR
    assert "--background-size" in SR.cli_args(plan) and "--background-size" not in SR.cli_args({"growth": 1.3})


def test_the_multiregion_mesher_takes_its_wall_size_and_keeps_a_thin_regions_level():
    req, _d, _ = _read("wall cells 1 mm, min cell size 2 mm",
                       sizes=[{"what": "near_wall", "value": 1, "unit": "mm", "quote": "wall cells 1 mm"}])
    ask = CR.Ask(req, "snappy_multiregion")
    base, lv, levels = CR.multiregion_sizes(ask, 0.016, (2, 2), extent=[0.5, 0.5, 0.5], regions=["air", "chip"])
    assert base == 0.016 and lv == (4, 4) and levels == {}
    req, _d, _ = _read("min cell size 2 mm", sizes=[{"what": "min_edge", "value": 2, "unit": "mm",
                                                      "quote": "min cell size 2 mm"}])
    ask = CR.Ask(req, "snappy_multiregion")
    lv, rr = CR.multiregion_cap(ask, 0.016, (4, 4), {"chip": [5, 5], "air": [4, 4]}, {"chip": {}})
    assert lv == (3, 3) and rr == {"chip": [5, 5], "air": [3, 3]}
    assert ask.rows[ask.rid("min_edge")]["status"] == "nearest"


# ------------------------------------------------------------------ cell measures ---------------------------------
def _box_mesh(case: Path, n=3, shear=0.0):
    """An n x n x n hex block as an ascii polyMesh (owner/neighbour ordered as OpenFOAM writes them)."""
    pm = case / "constant" / "polyMesh"
    pm.mkdir(parents=True)
    idx = {}
    pts = []
    for k in range(n + 1):
        for j in range(n + 1):
            for i in range(n + 1):
                idx[(i, j, k)] = len(pts)
                pts.append((i + shear * j, j, k))
    cell = {(i, j, k): c for c, (i, j, k) in enumerate((i, j, k) for k in range(n) for j in range(n) for i in range(n))}
    internal, boundary = [], []
    for (i, j, k), c in cell.items():
        for d, (a, b) in enumerate([((1, 0, 0), None), ((0, 1, 0), None), ((0, 0, 1), None)]):
            q = (i + a[0], j + a[1], k + a[2])
            if d == 0:
                f = [(i + 1, j, k), (i + 1, j + 1, k), (i + 1, j + 1, k + 1), (i + 1, j, k + 1)]
            elif d == 1:
                f = [(i, j + 1, k), (i, j + 1, k + 1), (i + 1, j + 1, k + 1), (i + 1, j + 1, k)]
            else:
                f = [(i, j, k + 1), (i + 1, j, k + 1), (i + 1, j + 1, k + 1), (i, j + 1, k + 1)]
            if q in cell:
                internal.append((c, cell[q], [idx[v] for v in f]))
            else:
                boundary.append((c, [idx[v] for v in f]))
        for d, f in ((0, [(i, j, k), (i, j, k + 1), (i, j + 1, k + 1), (i, j + 1, k)]),
                     (1, [(i, j, k), (i + 1, j, k), (i + 1, j, k + 1), (i, j, k + 1)]),
                     (2, [(i, j, k), (i, j + 1, k), (i + 1, j + 1, k), (i + 1, j, k)])):
            if (i, j, k)[d] == 0:
                boundary.append((c, [idx[v] for v in f]))
    internal.sort(key=lambda t: (t[0], t[1]))
    head = "FoamFile\n{{ version 2.0; format ascii; class {c}; object {o}; }}\n"
    faces = [f for _, _, f in internal] + [f for _, f in boundary]
    (pm / "points").write_text(head.format(c="vectorField", o="points") + f"\n{len(pts)}\n(\n"
                               + "\n".join(f"({x} {y} {z})" for x, y, z in pts) + "\n)\n")
    (pm / "faces").write_text(head.format(c="faceList", o="faces") + f"\n{len(faces)}\n(\n"
                              + "\n".join(f"4({' '.join(map(str, f))})" for f in faces) + "\n)\n")
    own = [o for o, _, _ in internal] + [o for o, _ in boundary]
    (pm / "owner").write_text(head.format(c="labelList", o="owner") + f"\n{len(own)}\n(\n"
                              + "\n".join(map(str, own)) + "\n)\n")
    (pm / "neighbour").write_text(head.format(c="labelList", o="neighbour") + f"\n{len(internal)}\n(\n"
                                  + "\n".join(str(nb) for _, nb, _ in internal) + "\n)\n")


def test_a_cube_grid_measures_as_checkmesh_does(tmp_path):
    _box_mesh(tmp_path)
    f = CM.measure(tmp_path)
    assert f["n_cells"] == 27
    assert np.allclose(f["non_orthogonality"], 0.0, atol=1e-6) and np.allclose(f["skewness"], 0.0, atol=1e-6)
    assert np.allclose(f["aspect_ratio"], 1.0) and np.allclose(f["determinant"], 1.0)
    assert np.allclose(f["min_volume"], 1.0) and np.allclose(f["orthogonal_quality"], 1.0)


def test_a_sheared_grid_reads_its_angle(tmp_path):
    _box_mesh(tmp_path, shear=0.5)
    f = CM.measure(tmp_path)
    worst = CM.worst_of(f)
    assert 20.0 < worst["non_orthogonality"] < 30.0        # atan(0.5) = 26.6 degrees between centres and normal
    assert worst["orthogonal_quality"] < 0.95 and np.allclose(f["min_volume"], 1.0)


# ------------------------------------------------------------------ CFDREQ-6: asked against delivered ---------------
def test_the_report_rows_reach_the_verdict_and_a_miss_is_a_note(tmp_path):
    from meshpipeline.engines import verdict_limits as VL

    _box_mesh(tmp_path, shear=0.5)
    req, _d, _ = _read("non-ortho under 10, max 1M cells, 5 layers",
                       counts=[{"what": "ceiling", "value": 1e6, "quote": "max 1M cells"}],
                       layers=[{"what": "count", "value": 5, "unit": "none", "quote": "5 layers"}],
                       limits=[_lim("non_orthogonality", 10, "non-ortho under 10")])
    MR.write(tmp_path, MR.section("used", requirements=req))
    ask = CR.Ask(req, "snappy")
    ask.budget(2_000_000, hard=8_000_000)
    ask.done("layers:count", "5 layers on every wall")
    ask.write(tmp_path)
    q = CR.attach(tmp_path, {"cells": 27, "max_non_ortho": 26.6, "layer_coverage_pct": 91.0}, "snappy")
    rows = {r["field"]: r for r in q["requirements"]["rows"]}
    assert rows["cells:ceiling"]["met"] is True and rows["layers:count"]["met"] is True
    assert rows["limit:non_orthogonality"]["met"] is False and "cells" in rows["limit:non_orthogonality"]["delivered"]
    inputs = VL.inputs_for(engine="snappy", topology="external", manifest={"quality": q}, valid=True)
    mine = [r for r in inputs["limits"] if r["key"].startswith("asked_")]
    assert len(mine) == 3 and sum(1 for r in mine if r["measured"]) == 1
    facts = CR.facts_text({"quality": q})
    assert facts.startswith("WHAT THE USER'S NOTES ASKED") and "Non-orthogonality under 10°: not fully met" in facts
    assert CR.facts_text({"quality": {"cells": 1}}) == ""
    assert any("From your notes" in t or t for t in __import__(
        "meshpipeline.contracts.user_notes", fromlist=["x"]).read_remarks(tmp_path))


def test_nothing_read_leaves_the_quality_record_as_it_was(tmp_path):
    q = {"cells": 10, "max_non_ortho": 30.0}
    assert CR.attach(tmp_path, q, "snappy") is q
    from meshpipeline.engines import verdict_limits as VL
    inputs = VL.inputs_for(engine="snappy", topology="external", manifest={"quality": q}, valid=True)
    assert not any(r["key"].startswith("asked_") for r in inputs["limits"])


def test_an_error_of_ours_never_fails_the_mesh_and_is_told():
    req, _d, _ = _read("surface cells 2 mm", sizes=[{"what": "near_wall", "value": 2, "unit": "mm",
                                                      "quote": "surface cells 2 mm"}])
    ask = CR.Ask(req, "snappy")
    # a broken input inside the step: the engine's own levels come back, and the row says why nothing was applied
    got = CR.snappy_levels(ask, None, floor=3, level=4, flevel=5, regions=[], part_levels=False)
    assert got == (10, 4, 4, 5)
    assert ask.rows[ask.rid("near_wall")]["status"] == "not_applied"
    assert "error of ours" in ask.rows[ask.rid("near_wall")]["built"]
    assert CR.snappy_dict(ask, "not a dict at all", wall_cell_m=None) == "not a dict at all"


def test_a_y_plus_with_the_flows_numbers_is_the_first_layer_snappy_builds(tmp_path):
    req, _d, _ = _read("8 layers, y+ around 1. Air at 30 m/s, chord 0.244 m.",
                       layers=[{"what": "count", "value": 8, "unit": "none", "quote": "8 layers"},
                               {"what": "y_plus", "value": 1, "unit": "none", "quote": "y+ around 1"}],
                       physics=[{"what": "air", "value": 0, "unit": "none", "quote": "Air at 30 m/s"},
                                {"what": "velocity", "value": 30, "unit": "m/s", "quote": "Air at 30 m/s"},
                                {"what": "length", "value": 0.244, "unit": "m", "quote": "chord 0.244 m"}])
    ask = CR.Ask(req, "snappy")
    text, _s = _snappy(tmp_path / "y", ask)
    h = re_num(text, "firstLayerThickness")
    assert 5e-6 < h < 3e-5 and "relativeSizes false;" in text
    row = ask.rows[ask.rid("layers:y_plus")]
    assert row["status"] == "applied" and row["first_m"] == float(f"{h:.4g}")
