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
    assert h == pytest.approx(ask.how["visc_m"], rel=1e-4)                 # y+ 1 at the first cell's top
    assert ask.fallback is not None and ask.fallback["why"] == "laminar"
    delta = CR.boundary_layer_m(ask.how)
    assert delta == pytest.approx(5.0 * 0.06 / math.sqrt(ask.how["reynolds"]), rel=1e-9)
    n = int(_num(out, "nSurfaceLayers"))
    assert n > 5 and CR.cells_across(h, 1.2, n, 0.0016, delta) >= CR.BL_CELLS
    row = _y_row(ask)
    assert row["status"] == "nearest" and row["treatment"] == "resolved" and row["fallback"] == "laminar"
    assert row["built"].startswith("This flow is too slow, or the body too small, for wall functions (Re 4")
    assert "the wall is resolved instead: first cell" in row["built"] and f"{n} layers" in row["built"]
    assert "cells across its boundary layer" in row["built"] and row["cells_across_bl"] >= CR.BL_CELLS


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


def test_the_stack_never_grows_past_what_the_wall_cells_hold():
    ask = _ask("10 m/s", 0.06)
    out = CR.snappy_dict(ask, SHM, wall_cell_m=0.0003)                     # fine cells: the core resolves the layer
    n = int(_num(out, "nSurfaceLayers"))
    h = _num(out, "firstLayerThickness")
    stack = h * (1.2 ** n - 1) / 0.2
    ours = 0.3 * 0.0003 * sum(1.2 ** -k for k in range(5))
    assert n >= 5 and stack <= max(ours, h * (1.2 ** 5 - 1) / 0.2) * (1 + 1e-9)
    assert CR.cells_across(h, 1.2, n, 0.0003, CR.boundary_layer_m(ask.how)) >= CR.BL_CELLS


def test_a_count_of_the_user_s_stands():
    ask = _ask("10 m/s", 0.06)
    ask.n_layers = 3
    out = CR.snappy_dict(ask, SHM.replace("nSurfaceLayers 5", "nSurfaceLayers 3"), wall_cell_m=0.0016)
    assert _num(out, "nSurfaceLayers") == 3 and ask.fallback is not None


def test_cfmesh_builds_the_resolved_fallback_too():
    ask = _ask("10 m/s", 0.06, engine="cfmesh")
    s = CR.cfmesh_strategy(ask, {"n_layers": 2}, length_m=0.06)
    h = s["first_layer_thickness"]
    assert h == pytest.approx(ask.how["visc_m"], rel=1e-9)
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
