# Responsibility: Verify CFDREQ-17 - a body in wall-function mode that wall functions do not suit (laminar on its length, or a wall-function cell too thick for its boundary layer) is resolved instead: an absolute y+ 1 first layer, enough layers for about 10 cells across its boundary layer, said in plain words; turbulent bodies keep their wall functions; internal flows are unchanged.
# Boundaries: pure over plain data and dictionary text; no model, no mesher is run.
from __future__ import annotations

import math
import re

import pytest

from meshpipeline.contracts import flow_conditions as FC
from meshpipeline.engines import cfd_requirements as CR
from meshpipeline.engines import mesh_requirements as MR


def _ext(speed: str) -> str:
    return ("External flow around the car, confirmed by the user on the geometry stage. "
            + FC.sentence("air at 20 °C", speed, "functions", internal=False, wall_said=False)
            + f" The flow travels along +x. {FC.ACCEPTED_LEAD} the fluid, its speed, the wall treatment.")


SHM = """addLayersControls
{
    relativeSizes true;
    layers { body { nSurfaceLayers 5; } }
    expansionRatio 1.2;
    finalLayerThickness 0.3;
    minThickness 0.1;
    nGrow 0;
}
meshQualityControls
{
    minVol 1e-13;
    minDeterminant 0.001;
    relaxed { maxNonOrtho 75; }
}
"""


def _ask(speed: str, length_m: float, engine: str = "snappy") -> CR.Ask:
    sec = CR.with_conditions(MR.section("none"), {"request_txt": _ext(speed), "intake_patches": [],
                                                  "purpose": "external_cfd"})
    ask = CR.Ask(MR.requirements_of(sec), engine)
    ask.length_m, ask.length_from = length_m, "the body's length"
    return ask


def _num(text: str, key: str) -> float | None:
    m = re.search(rf"\b{key}\s+([-+0-9.eE]+)\s*;", text)
    return float(m.group(1)) if m else None


def _y_row(ask: CR.Ask) -> dict:
    return ask.rows[ask.rid("layers:y_plus")]


def test_a_small_slow_body_is_resolved_not_left_in_the_buffer_layer():
    # the CFD board's abc 00050662: a 60 mm body at 10 m/s in air, Re 4e4 - the old plan kept snappy's relative layers
    # (0.169 of the cell: y+ 2-5); now an absolute y+ 1 first layer and the boundary layer resolved
    ask = _ask("10 m/s", 0.06)
    out = CR.snappy_dict(ask, SHM, wall_cell_m=0.0016)
    assert "relativeSizes false;" in out
    h = _num(out, "firstLayerThickness")
    assert ask.fallback is not None and ask.fallback["why"] == "laminar"
    delta = CR.boundary_layer_m(ask.how)
    assert delta == pytest.approx(5.0 * 0.06 / math.sqrt(ask.how["reynolds"]), rel=1e-9)
    # CFDREQ-22: sized on the layer a fifth of the way along the body, not from y+ 1
    bl = CR.laminar_bl_m(ask.how)
    assert bl == pytest.approx(delta * math.sqrt(CR.BL_SIZE_AT), rel=1e-9)
    n = int(_num(out, "nSurfaceLayers"))
    assert _num(out, "expansionRatio") == pytest.approx(1.2)
    # 1.6 mm wall cells are thicker than the 0.67 mm layer: the stack alone fills it, at most BL_CELLS layers
    assert n == CR.BL_CELLS and h == pytest.approx(bl / CR.stack_height(1.0, 1.2, n), rel=1e-4)
    assert CR.cells_across(h, 1.2, n, 0.0016, bl) == pytest.approx(CR.BL_CELLS, abs=1e-3)
    row = _y_row(ask)
    # what the accepted proposal promised ("... the wall is resolved instead, and the result says which"): applied
    assert row["status"] == "applied" and row["treatment"] == "resolved" and row["fallback"] == "laminar"
    assert row["built"].startswith("the flow over this body is laminar at this speed (Re 4")
    assert "wall functions need a turbulent one, so the wall is resolved instead" in row["built"]
    assert f"{n} layers from" in row["built"] and "about 10 cells across it" in row["built"]
    assert row["bl_delta_m"] == pytest.approx(bl, rel=1e-3) and row["cells_across_bl"] >= CR.BL_CELLS - 0.05


def test_wall_functions_the_user_chose_on_a_small_body_are_the_nearest_there_is():
    # said in so many words (the conditions step's button), not only accepted: never a silent switch
    txt = ("External flow around the car, confirmed by the user on the geometry stage. "
           + FC.sentence("air at 20 °C", "10 m/s", "functions", internal=False, wall_said=True)
           + " The flow travels along +x.")
    sec = CR.with_conditions(MR.section("none"), {"request_txt": txt, "intake_patches": [], "purpose": "external_cfd"})
    ask = CR.Ask(MR.requirements_of(sec), "snappy")
    assert ask.treatment == "functions" and ask.treatment_said
    ask.length_m, ask.length_from = 0.06, "the body's length"
    out = CR.snappy_dict(ask, SHM, wall_cell_m=0.0016)
    bl = CR.laminar_bl_m(ask.how)
    assert _num(out, "firstLayerThickness") == pytest.approx(bl / CR.stack_height(1.0, 1.2, CR.BL_CELLS), rel=1e-4)
    row = _y_row(ask)
    assert row["status"] == "nearest" and row["fallback"] == "laminar" and "resolved instead" in row["built"]
    assert "not possible as asked" in " ".join(ask.lines())


def test_a_typed_wall_function_y_plus_on_a_small_body_is_the_nearest_there_is():
    from tests.unit.engines.test_cfd_requirements import _read
    req, _d, _ = _read("y+ 30 to 100", layers=[{"what": "y_plus", "value": 30, "unit": "none", "quote": "y+ 30 to 100"}])
    sec = CR.with_conditions(MR.section("used", requirements=req), {"request_txt": _ext("10 m/s"),
                                                                    "intake_patches": [], "purpose": "external_cfd"})
    ask = CR.Ask(MR.requirements_of(sec), "snappy")
    assert ask.treatment is None and ask.y_plus == 30
    ask.length_m, ask.length_from = 0.06, "the body's length"
    out = CR.snappy_dict(ask, SHM, wall_cell_m=0.0016)
    assert "relativeSizes false;" in out
    bl = CR.laminar_bl_m(ask.how)
    assert _num(out, "firstLayerThickness") == pytest.approx(bl / CR.stack_height(1.0, 1.2, CR.BL_CELLS), rel=1e-4)
    row = _y_row(ask)
    assert row["status"] == "nearest" and row["treatment"] == "resolved" and "laminar at this speed" in row["built"]
    # on a body wall functions suit, the typed y+ is built as typed
    big = CR.Ask(MR.requirements_of(sec), "snappy")
    big.length_m, big.length_from = 4.0, "the body's length"
    h = big.first_layer(length_m=4.0, length_from="the body's length")
    assert big.fallback is None and CR.y_plus_of(h, big.how) == pytest.approx(30.0)


def test_a_turbulent_body_keeps_its_wall_functions():
    ask = _ask("20 m/s", 4.0)                                                # a car: Re 5.3e6
    h = CR.y_plus_height(50.0, ask.req["physics"], length_m=4.0)[0]
    out = CR.snappy_dict(ask, SHM, wall_cell_m=20 * h)
    assert ask.fallback is None and _num(out, "firstLayerThickness") == pytest.approx(h, rel=1e-5)
    row = _y_row(ask)
    assert row["status"] == "applied" and row["treatment"] == "functions" and row["y_plus_expected"] == 50


def test_a_wall_function_cell_too_thick_for_a_turbulent_layer_is_resolved_too(monkeypatch):
    # a guard near transition: with the share set tight the turbulent layer cannot hold the cell
    monkeypatch.setattr(CR, "WALL_FUNCTION_BL_SHARE", 0.01)
    ask = _ask("20 m/s", 4.0)
    h = ask.first_layer(length_m=ask.length_m, length_from=ask.length_from)
    assert ask.fallback is not None and ask.fallback["why"] == "thick"
    assert h == pytest.approx(ask.how["visc_m"], rel=1e-9)
    assert "would be too thick for this body's boundary layer" in _y_row(ask)["built"]


def test_fine_wall_cells_let_a_short_stack_resolve_the_layer():
    # CFDREQ-22: with wall cells fine against the layer the core cells inside it count, so fewer layers (at most
    # LAMINAR_LAYERS_KEPT) from a thicker first layer put BL_CELLS across it
    ask = _ask("10 m/s", 0.06)
    out = CR.snappy_dict(ask, SHM, wall_cell_m=0.0001)
    n = int(_num(out, "nSurfaceLayers"))
    h = _num(out, "firstLayerThickness")
    bl = CR.laminar_bl_m(ask.how)
    assert n <= CR.LAMINAR_LAYERS_KEPT
    assert CR.cells_across(h, 1.2, n, 0.0001, bl) >= CR.BL_CELLS - 1e-3
    assert h * 1.2 ** (n - 1) <= CR.FIRST_CAP_REL * 0.0001 * (1 + 1e-4)     # its top layer under the wall cell's cap


def test_a_count_of_the_user_s_stands():
    ask = _ask("10 m/s", 0.06)
    ask.n_layers = 3
    out = CR.snappy_dict(ask, SHM.replace("nSurfaceLayers 5", "nSurfaceLayers 3"), wall_cell_m=0.0016)
    assert _num(out, "nSurfaceLayers") == 3 and ask.fallback is not None


def test_cfmesh_builds_the_resolved_fallback_too():
    ask = _ask("10 m/s", 0.06, engine="cfmesh")
    s = CR.cfmesh_strategy(ask, {"n_layers": 2}, length_m=0.06)
    h = s["first_layer_thickness"]
    # CFDREQ-22: before the wall cells are known, BL_CELLS layers filling the laminar layer
    assert h == pytest.approx(CR.laminar_bl_m(ask.how) / CR.stack_height(1.0, CR.LAMINAR_GROWTH, CR.BL_CELLS),
                              rel=1e-9)
    out = CR.cfmesh_layer_fit(ask, s, 0.0016)
    assert out["first_layer_thickness"] == pytest.approx(h) and out["n_layers"] > 2
    assert _y_row(ask)["treatment"] == "resolved"


def test_the_wall_level_is_chosen_with_the_wall_function_first_layer():
    ask = _ask("20 m/s", 4.0)                                                # y+ 50: a first layer of about 2 mm
    h = CR.y_plus_height(50.0, ask.req["physics"], length_m=4.0)[0]
    base = 0.256                                                             # level 7: 2 mm cells, too fine for it
    lv = CR.wall_function_level(ask, base, level=7, floor=3)
    assert base / 2 ** lv >= h / CR.FIRST_CAP_REL and base / 2 ** (lv + 1) < h / CR.FIRST_CAP_REL
    assert lv < 7 and "fits under them" in ask.wall_level_note
    # never coarser than 100 cells along the body, nor than the sealing floor
    assert CR.wall_function_level(_ask("20 m/s", 4.0), base, level=7, floor=7) == 7
    short = _ask("20 m/s", 0.9)                                              # Re 1.2e6: 9 mm at most along it
    assert base / 2 ** CR.wall_function_level(short, base, level=7, floor=0) <= 0.9 / CR.BODY_CELLS_ALONG
    # a first layer that fits, a laminar body, a wall size of the user's: unchanged
    assert CR.wall_function_level(_ask("20 m/s", 4.0), base, level=3, floor=0) == 3
    assert CR.wall_function_level(_ask("10 m/s", 0.06), base, level=7, floor=0) == 7
    mine = _ask("20 m/s", 4.0)
    mine.wall_m = 0.002
    assert CR.wall_function_level(mine, base, level=7, floor=0) == 7
    # the row says it
    out = CR.snappy_dict(ask, SHM, wall_cell_m=base / 2 ** lv)
    assert _num(out, "firstLayerThickness") == pytest.approx(h, rel=1e-5)
    assert "fits under them" in _y_row(ask)["built"] and _y_row(ask)["status"] == "applied"


def test_cells_across_counts_layers_then_core_cells():
    assert CR.cells_across(0.1, 1.0, 5, 1.0, 0.25) == pytest.approx(2.5)
    assert CR.cells_across(0.1, 1.0, 2, 0.1, 0.5) == pytest.approx(5.0)
    assert CR.boundary_layer_m({"length_m": 1.0, "reynolds": 1e6, "laminar": False}) == pytest.approx(0.37 * 1e6 ** -0.2, rel=1e-9)


def test_the_laminar_stack_is_sized_on_its_boundary_layer():
    # CFDREQ-22: coarse wall cells (thicker than the layer): the stack alone fills it, BL_CELLS layers
    n, h, across = CR.laminar_stack(0.002, 0.003, 1.2)
    assert n == CR.BL_CELLS and across == pytest.approx(CR.BL_CELLS, abs=1e-6)
    assert h == pytest.approx(0.002 / CR.stack_height(1.0, 1.2, CR.BL_CELLS))
    # fine wall cells: the core cells inside it count - at most LAMINAR_LAYERS_KEPT layers, a thicker first layer
    n, h, across = CR.laminar_stack(0.002, 0.0004, 1.2)
    assert 1 < n <= CR.LAMINAR_LAYERS_KEPT and across >= CR.BL_CELLS - 1e-6
    # the thickest that still does it - or the thickest its top layer's cap (FIRST_CAP_REL of the wall cell) allows
    assert (CR.cells_across(h * 1.01, 1.2, n, 0.0004, 0.002) < CR.BL_CELLS
            or h * 1.2 ** (n - 1) == pytest.approx(CR.FIRST_CAP_REL * 0.0004))
    assert CR.laminar_stack(0.002, 0.0004, 1.2, n_min=n - 1, n_max=n - 1)[2] < CR.BL_CELLS   # and the fewest layers
    # cells a tenth of the layer: the core resolves it, one layer under the wall cell's cap
    n, h, across = CR.laminar_stack(0.002, 0.0002, 1.2)
    assert n == 1 and h == pytest.approx(CR.FIRST_CAP_REL * 0.0002) and across >= CR.BL_CELLS
    # a count of the user's stands; the cells it reaches are what it reaches
    n, h, across = CR.laminar_stack(0.002, 0.003, 1.2, n_fixed=4)
    assert n == 4 and across < CR.BL_CELLS
    # a tight wall's cap thins the first layer, down to LAMINAR_CAP_THIN of the floor, and the stack still reaches it
    floor = 0.002 / CR.stack_height(1.0, 1.2, CR.BL_CELLS)
    n, h, across = CR.laminar_stack(0.002, 0.003, 1.2, stack_max_m=0.001)
    assert CR.stack_height(h, 1.2, n) <= 0.001 * (1 + 1e-6) and n == CR.BL_CELLS and across >= CR.BL_CELLS - 1e-6
    assert floor * CR.LAMINAR_CAP_THIN <= h < floor
    # ... and past that it cuts layers, never the first layer: fewer cells across, said
    n, h, across = CR.laminar_stack(0.002, 0.003, 1.2, stack_max_m=0.0002)
    assert CR.stack_height(h, 1.2, n) <= 0.0002 * (1 + 1e-6) and n < CR.BL_CELLS and across < CR.BL_CELLS
    assert h >= floor * CR.LAMINAR_CAP_THIN * (1 - 1e-9)
    # a cap under even one such layer (a sliver's reading, abc 00067838: 3e-7 m) is set aside: no 1e-8 m layers
    n, h, across = CR.laminar_stack(0.002, 0.003, 1.2, stack_max_m=3e-7)
    assert n == CR.BL_CELLS and h == pytest.approx(floor) and across >= CR.BL_CELLS - 1e-6
    n, h, across = CR.laminar_stack(0.002, 0.003, 1.2, n_fixed=4)
    words = CR.fallback_words(_ask_lam(), h, n, across)
    assert "not the 10 a resolved layer needs" in words


def _ask_lam() -> CR.Ask:
    ask = _ask("10 m/s", 0.06)
    ask.first_layer(length_m=ask.length_m, length_from=ask.length_from)
    return ask


def test_the_laminar_row_is_judged_by_the_cells_across_its_layer(monkeypatch, tmp_path):
    # CFDREQ-22: met with BL_CELLS (to the nearest cell) across the layer on 80% of the wall; else missed, said
    p = {"bl_delta_m": 0.0007, "fallback": "laminar", "first_m": 2.6e-5}
    monkeypatch.setattr(CR, "built_bl_cells", lambda ws, d: {"bl_area_met_pct": 86.0, "bl_cells_median": 10.2,
                                                             "bl_cells_p10": 7.0})
    row = CR._bl_cells_row(tmp_path, p)
    assert row["met"] is True and "on 86% of the wall" in row["delivered"]
    monkeypatch.setattr(CR, "built_bl_cells", lambda ws, d: {"bl_area_met_pct": 52.0, "bl_cells_median": 6.1,
                                                             "bl_cells_p10": 1.0})
    row = CR._bl_cells_row(tmp_path, p)
    assert row["met"] is False and "under the 80% needed" in row["delivered"]
    monkeypatch.setattr(CR, "built_bl_cells", lambda ws, d: {})
    assert CR._bl_cells_row(tmp_path, p) is None                               # not counted: judged as before
    assert CR._bl_cells_row(tmp_path, {"fallback": "laminar"}) is None


def test_the_laminar_band_is_used_when_the_mesh_can_afford_it_and_priced_when_not():
    # CFDREQ-23: wall cells half the layer's thickness - a band two levels finer lets at most 6 layers reach 10 across
    floor = 0.002 / CR.stack_height(1.0, 1.2, CR.BL_CELLS)
    b = CR.laminar_band(0.002, 0.001, 1.2, faces=1000, room=1e9, n_now=10)
    assert b["used"] and b["levels"] == 2 and b["cell_m"] == pytest.approx(0.00025)
    assert b["layers"] <= CR.LAMINAR_BAND_LAYERS and b["across"] >= CR.BL_CELLS - 1e-9
    assert b["first_m"] >= floor * (1 - 1e-9)                       # never thinner than the plan without the band
    assert b["cells"] > 0
    # the same band over the room the wall may add: not used, its cost said
    poor = CR.laminar_band(0.002, 0.001, 1.2, faces=1000, room=b["cells"] - 1, n_now=10)
    assert not poor["used"] and poor["cells"] == b["cells"]
    # wall cells fifteen times the layer: no band within LAMINAR_BAND_LEVELS reaches it
    far = CR.laminar_band(0.002, 0.03, 1.2, faces=1000, room=1e12, n_now=10)
    assert not far["used"] and far["cells"] is None
    assert not CR.laminar_band(0.002, 0.0, 1.2, faces=1000, room=1e9, n_now=10)["used"]


def test_the_band_goes_into_the_wall_surfaces_distance_region():
    shm = ("refinementSurfaces { body { level (5 5); } } resolveFeatureAngle 30;\n"
           "refinementRegions { body { mode distance; levels ((0.01 6) (0.08 4)); } wake0 { mode inside; "
           "levels ((1e15 3)); } }\n")
    out = CR.band_regions(shm, 0.002, 2)
    assert "levels ((0.002 7) (0.01 6) (0.08 4));" in out and "wake0 { mode inside; levels ((1e15 3)); }" in out
    # a band deeper than the near band: the bands it covers are dropped, the order kept
    assert "levels ((0.05 7) (0.08 4));" in CR.band_regions(shm, 0.05, 2)
    assert CR.band_regions("refinementRegions { }", 0.002, 2) is None


SHM_EXT = ("castellatedMeshControls { maxLocalCells 1000000; maxGlobalCells 1000000;\n"
           "  refinementSurfaces { body { level (5 5); } }\n"
           "  refinementRegions { body { mode distance; levels ((0.01 6) (0.08 4)); } } }\n"
           + SHM.replace("nGrow 0;", "nGrow 0;\n    nBufferCellsNoExtrude 0;"))


def test_a_laminar_snappy_stack_keeps_its_layers_and_gets_the_band_it_can_afford():
    delta = float(_ask_lam().fallback["bl_delta_m"])
    # the buffer stays as the case has it: -1 lost the 10-cell wall on 6 of 12 sample parts and ran 2-10x slower
    ask = _ask_lam()
    out = CR.snappy_dict(ask, SHM_EXT, wall_cell_m=delta * 2)
    assert "nBufferCellsNoExtrude 0;" in out and "nBufferCellsNoExtrude -1" not in out
    assert "levels ((0.01 6) (0.08 4));" in out
    # wall cells half the layer: the band two levels finer, a short stack, the castellation ceiling raised for it
    ask = _ask_lam()
    out = CR.snappy_dict(ask, SHM_EXT, wall_cell_m=delta / 2, bridge={"faces": 1000.0, "room": 1e9}, band=True)
    band = ask.fallback["band"]
    assert band["levels"] == 2 and f"levels (({delta:.6g} 7)" in out
    assert _num(out, "nSurfaceLayers") <= CR.LAMINAR_BAND_LAYERS and ask.fallback["cells_across"] >= CR.BL_CELLS
    assert _num(out, "maxGlobalCells") == 1000000 + band["cells"]
    # over the room: no band, the stack on the wall cells, and the row says why if it is not kept
    ask = _ask_lam()
    out = CR.snappy_dict(ask, SHM_EXT, wall_cell_m=delta / 2, bridge={"faces": 1000.0, "room": 20000.0}, band=True)
    assert "band" not in ask.fallback and "levels ((0.01 6) (0.08 4));" in out
    assert "more than the" in ask.fallback["band_note"] and _y_row(ask)["band_note"] == ask.fallback["band_note"]
    # not the external body's case: no band
    ask = _ask_lam()
    CR.snappy_dict(ask, SHM_EXT, wall_cell_m=delta / 2, bridge={"faces": 1000.0, "room": 1e9})
    assert "band" not in ask.fallback


def test_the_missed_laminar_row_says_why_no_band(monkeypatch, tmp_path):
    p = {"bl_delta_m": 0.0007, "fallback": "laminar", "band_note": "cells 4 times finer near the wall would"}
    monkeypatch.setattr(CR, "built_bl_cells", lambda ws, d: {"bl_area_met_pct": 52.0, "bl_cells_median": 6.1})
    assert CR._bl_cells_row(tmp_path, p)["delivered"].endswith("cells 4 times finer near the wall would")
