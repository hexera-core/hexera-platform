# Responsibility: Verify CFDREQ-13 - the wall treatment the user accepted ("OK": wall functions) or chose ("resolve": y+ about 1) sizes the first layer from the run's own conditions on snappyHexMesh, cfMesh and VMTK, fitted to the wall cells: in the wall-function band, else the nearest that fits, else a resolved wall; laminar flow has no target; a typed y+, first height or layer count still wins; the y+ said is the one the same estimate gives.
# Boundaries: pure over plain data and dictionary text; no model, no mesher is run.
from __future__ import annotations

import math
import re

import pytest
from tests.unit.engines.test_cfd_requirements import _read

from meshpipeline.contracts import flow_conditions as FC
from meshpipeline.engines import cfd_requirements as CR
from meshpipeline.engines import mesh_requirements as MR

OK_INTERNAL = ("Internal flow through the pipe, confirmed by the user on the geometry stage. "
               + FC.sentence("water at 20 °C", "1 m/s", "functions", internal=True, wall_said=False)
               + f" Openings: inlet (inlet, 200 mm across). {FC.ACCEPTED_LEAD} the fluid, its speed, the wall treatment.")
RESOLVED_INTERNAL = ("Internal flow through the pipe, confirmed by the user on the geometry stage. "
                     + FC.sentence("water at 20 °C", "1 m/s", "resolved", internal=True) + " Openings: inlet (inlet).")
OK_EXTERNAL = ("External flow around the car, confirmed by the user on the geometry stage. "
               + FC.sentence("air at 20 °C", "20 m/s", "functions", internal=False, wall_said=False)
               + f" The flow travels along +x. {FC.ACCEPTED_LEAD} the fluid, its speed, the wall treatment.")
PATCHES = [{"name": "inlet", "type": "inlet", "diameter_mm": 200.0}, {"name": "outlet", "type": "outlet"}]

SHM = """addLayersControls
{
    relativeSizes true;
    layers { wall { nSurfaceLayers 5; } }
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


def _ask(request_txt: str, engine: str, *, patches=PATCHES, purpose: str = "internal_cfd", sec=None) -> CR.Ask:
    sec = CR.with_conditions(sec or MR.section("none"), {"request_txt": request_txt, "intake_patches": patches,
                                                         "purpose": purpose})
    return CR.Ask(MR.requirements_of(sec), engine)


def _num(text: str, key: str) -> float | None:
    m = re.search(rf"\b{key}\s+([-+0-9.eE]+)\s*;", text)
    return float(m.group(1)) if m else None


def _y_row(ask: CR.Ask) -> dict:
    return ask.rows[ask.rid("layers:y_plus")]


# ------------------------------------------------------------------ the ask the conditions make --------------------
def test_ok_on_the_proposal_is_a_wall_function_ask_on_the_passage_s_own_diameter():
    ask = _ask(OK_INTERNAL, "snappy")
    assert ask.y_plus == 50.0 and ask.treatment == "functions"
    ph = ask.req["physics"]
    assert ph["flow"] == "internal" and ph["hydraulic_diameter_m"] == pytest.approx(0.2)
    h, missing, how = CR.y_plus_height(50.0, ph)
    assert missing == [] and how["length_from"] == "the inlet's hydraulic diameter"
    # the smooth-pipe estimate at the cell centre: Re 2e5, Haaland, h = 2 y+ nu / u_tau
    re_n = 1.0 * 0.2 / FC.NU["water"]
    f = (1.0 / (-1.8 * math.log10(6.9 / re_n))) ** 2
    assert h == pytest.approx(2 * 50 * FC.NU["water"] / (1.0 * math.sqrt(f / 8)), rel=1e-9)
    assert "the proposal you accepted" in MR.words(CR.with_conditions(MR.section("none"), {
        "request_txt": OK_INTERNAL, "intake_patches": PATCHES, "purpose": "internal_cfd"}))[0]


def test_the_y_plus_said_is_the_one_the_same_estimate_gives():
    ask = _ask(OK_INTERNAL, "snappy")
    h = ask.first_layer()
    assert CR.y_plus_of(h, ask.how, 50.0) == pytest.approx(50.0)
    assert CR.y_plus_of(h / 2, ask.how, 50.0) == pytest.approx(25.0)


# ------------------------------------------------------------------ wall_fit: the three branches ------------------
def test_wall_fit_keeps_a_first_layer_that_fits_takes_the_thickest_in_band_else_resolves():
    ask = _ask(OK_INTERNAL, "snappy")
    h = ask.first_layer()                                              # about 2.3 mm for y+ 50
    fits = CR.wall_fit(ask, h, 10 * h)
    assert fits == {"treatment": "functions", "first_m": h, "y_plus": 50.0, "why": ""}
    cap = CR.wall_fit(ask, h, h / 0.6 * 0.8)                           # room for 0.8 h: y+ 40
    assert cap["treatment"] == "functions" and cap["y_plus"] == pytest.approx(40.0) and "no thicker" in cap["why"]
    tight = CR.wall_fit(ask, h, h)                                     # room for 0.6 h: y+ 30 exactly is in band
    assert tight["treatment"] == "functions" and tight["y_plus"] == pytest.approx(30.0)
    narrow = CR.wall_fit(ask, h, 0.5 * h)                              # room for 0.3 h: y+ 15, the buffer layer
    # resolved: y+ 1 at the first cell's top, as a typed y+ 1 is built (its centre at about 0.5)
    assert narrow["treatment"] == "resolved" and narrow["y_plus"] == pytest.approx(0.5)
    assert narrow["first_m"] == pytest.approx(h / 100.0) and "buffer layer" in narrow["why"]


def test_laminar_flow_has_no_wall_function_target():
    tiny = [{"name": "inlet", "type": "inlet", "diameter_mm": 1.0}, {"name": "outlet", "type": "outlet"}]
    ask = _ask(OK_INTERNAL, "snappy", patches=tiny)                    # Re 1000 in a 1 mm channel
    assert ask.first_layer() is None and ask.laminar
    row = _y_row(ask)
    assert row["status"] == "applied" and "laminar at these conditions" in row["built"]
    out = CR.snappy_dict(ask, SHM, wall_cell_m=0.0001)
    assert "relativeSizes true;" in out and _num(out, "nSurfaceLayers") == 5          # our own layers


# ------------------------------------------------------------------ snappyHexMesh -------------------------------
def test_snappy_builds_the_wall_function_first_layer_and_says_the_y_plus():
    ask = _ask(OK_INTERNAL, "snappy")
    h = CR.y_plus_height(50.0, ask.req["physics"])[0]
    wall = 10 * h
    out = CR.snappy_dict(ask, SHM, wall_cell_m=wall)
    assert "relativeSizes false;" in out and _num(out, "firstLayerThickness") == pytest.approx(h, rel=1e-5)
    n = int(_num(out, "nSurfaceLayers"))
    assert n == CR.stack_count(h, wall, 1.2) and h * 1.2 ** (n - 1) <= 0.6 * wall
    row = _y_row(ask)
    assert row["status"] == "applied" and row["treatment"] == "functions" and row["y_plus_expected"] == 50
    assert row["built"].startswith("wall functions: first layer") and "y+ about 50 for water at 1 m/s" in row["built"]


def test_snappy_resolves_a_wall_too_tight_for_wall_functions_and_says_so():
    ask = _ask(OK_INTERNAL, "snappy")
    h = CR.y_plus_height(50.0, ask.req["physics"])[0]
    out = CR.snappy_dict(ask, SHM, wall_cell_m=0.5 * h)
    assert _num(out, "firstLayerThickness") == pytest.approx(h / 100.0, rel=1e-4)
    row = _y_row(ask)
    # CFDREQ-25: the accepted proposal promised a resolved wall where wall functions do not fit: applied, said with why
    assert row["status"] == "applied" and row["treatment"] == "resolved" and row["y_plus_expected"] == 0.5
    assert row["fallback"] == "cells" and "do not suit this wall" in row["built"]
    assert row["built"].startswith("a resolved wall") and "wall functions do not suit" in row["built"]


def test_snappy_resolved_wall_the_user_chose_is_y_plus_one_at_the_cells_top():
    ask = _ask(RESOLVED_INTERNAL, "snappy")
    assert ask.treatment == "resolved"
    out = CR.snappy_dict(ask, SHM, wall_cell_m=0.01)
    h1 = _num(out, "firstLayerThickness")
    assert h1 == pytest.approx(ask.how["visc_m"], rel=1e-4)            # y+ 1 at the top, as before
    assert CR.y_plus_of(h1, ask.how) == pytest.approx(0.5, rel=1e-3)   # its centre at about 0.5, said
    row = _y_row(ask)
    assert row["built"].startswith("a resolved wall") and "centre sits at y+ about 0.5" in row["built"]


@pytest.mark.parametrize(("typed", "centre", "buffer"), [
    (1.0, False, False),       # a resolved wall: the cell's full height, as #483 builds and its cases measure
    (10.0, True, True),        # the buffer layer: built at the centre, told it is there
    (30.0, True, False),       # a wall-function y+: at the centre - a height at y+ 30 puts the centre at 15
    (100.0, True, False),
])
def test_a_typed_y_plus_is_built_by_its_range_and_told_with_the_centre_s_y_plus(typed, centre, buffer):
    req, _d, _ = _read(f"y+ {typed:g}", layers=[{"what": "y_plus", "value": typed, "unit": "none", "quote": f"y+ {typed:g}"}])
    ask = _ask(OK_INTERNAL, "snappy", sec=MR.section("used", requirements=req))
    assert ask.treatment is None                                      # theirs: no wall-treatment fit
    h = ask.first_layer()
    visc = ask.how["visc_m"]
    assert h == pytest.approx((2.0 if centre else 1.0) * typed * visc, rel=1e-9)
    row = _y_row(ask)
    yc = CR.y_plus_of(h, ask.how)
    assert yc == pytest.approx(typed if centre else typed / 2, rel=1e-9) and row["y_plus_centre"] == float(f"{yc:.3g}")
    assert f"first layer {CR._mm(h)} high, its centre at y+ about" in row["built"]
    assert ("buffer layer" in row["built"]) is buffer


def test_a_typed_y_plus_first_height_or_count_still_wins():
    req, _d, _ = _read("y+ around 5", layers=[{"what": "y_plus", "value": 5, "unit": "none", "quote": "y+ around 5"}])
    ask = _ask(OK_INTERNAL, "snappy", sec=MR.section("used", requirements=req))
    assert ask.y_plus == 5 and ask.treatment is None                    # theirs: no wall-treatment fit
    req, _d, _ = _read("8 prism layers", layers=[{"what": "count", "value": 8, "unit": "none", "quote": "8 prism layers"}])
    ask = _ask(OK_INTERNAL, "snappy", sec=MR.section("used", requirements=req))
    assert ask.n_layers == 8 and ask.treatment == "functions"
    h = CR.y_plus_height(50.0, ask.req["physics"])[0]
    out = CR.snappy_dict(ask, SHM.replace("nSurfaceLayers 5", "nSurfaceLayers 8"), wall_cell_m=10 * h)
    assert _num(out, "nSurfaceLayers") == 8                              # the count asked stays


# ------------------------------------------------------------------ cfMesh -----------------------------------------
def test_cfmesh_counts_its_layers_so_the_split_stack_starts_at_the_height():
    ask = _ask(OK_INTERNAL, "cfmesh")
    s = CR.cfmesh_strategy(ask, {"n_layers": 2}, length_m=1.0)
    h = s["first_layer_thickness"]
    wall = 20 * h
    out = CR.cfmesh_layer_fit(ask, s, wall)
    n, g = out["n_layers"], 1.2
    stack = CR.CFMESH_STACK_REL * wall
    assert stack * (g - 1) / (g ** n - 1) >= h * (1 - 1e-9) > stack * (g - 1) / (g ** (n + 1) - 1)
    row = _y_row(ask)
    assert row["status"] == "applied" and row["y_plus_expected"] == pytest.approx(50, rel=0.01)
    # a wall cell too fine for it: the wall is resolved, said
    ask = _ask(OK_INTERNAL, "cfmesh")
    s = CR.cfmesh_strategy(ask, {"n_layers": 2}, length_m=1.0)
    out = CR.cfmesh_layer_fit(ask, s, 0.5 * s["first_layer_thickness"])
    assert out["first_layer_thickness"] == pytest.approx(s["first_layer_thickness"] / 100.0, rel=1e-6)
    assert _y_row(ask)["status"] == "applied" and _y_row(ask)["treatment"] == "resolved"     # CFDREQ-25: accepted
    # nothing of the wall treatment: unchanged
    req, _d, _ = _read("8 prism layers", layers=[{"what": "count", "value": 8, "unit": "none", "quote": "8 prism layers"}])
    plain = CR.Ask(req, "cfmesh")
    assert CR.cfmesh_layer_fit(plain, {"n_layers": 8}, 0.01) == {"n_layers": 8}


# ------------------------------------------------------------------ VMTK ---------------------------------------------
def test_vmtk_sizes_its_stack_from_the_same_number():
    ask = _ask(OK_INTERNAL, "vmtk")
    out = CR.vmtk_spec(ask, {"boundary_layers": 5, "boundary_layer_thickness_factor": 0.10,
                             "edge_length_factor": 0.15}, radius_m=0.1)
    h = CR.y_plus_height(50.0, ask.req["physics"])[0]
    n, t = out["boundary_layers"], out["boundary_layer_thickness_factor"]
    assert t <= CR.VMTK_STACK_MAX and 1 <= n <= 5
    built = t * 0.1 * (1.25 - 1) / (1.25 ** n - 1)
    assert built == pytest.approx(h, rel=2e-3)
    row = _y_row(ask)
    assert row["status"] == "applied" and row["treatment"] == "functions"
    # without the vessel's radius it says it has no such setting, as before
    ask = _ask(OK_INTERNAL, "vmtk")
    CR.vmtk_spec(ask, {"boundary_layers": 5})
    assert _y_row(ask)["status"] == "not_applied"


def test_vmtk_on_a_resolved_wall_keeps_its_stack_and_says_the_y_plus():
    ask = _ask(RESOLVED_INTERNAL, "vmtk")
    out = CR.vmtk_spec(ask, {"boundary_layers": 5, "boundary_layer_thickness_factor": 0.10,
                             "edge_length_factor": 0.15}, radius_m=0.1)
    assert out["boundary_layer_thickness_factor"] == 0.10
    row = _y_row(ask)
    assert row["status"] == "nearest" and row["y_plus_expected"] > 1


# ------------------------------------------------------------------ the engine choice -----------------------------
def test_wall_functions_keep_vmtk_a_resolved_wall_moves_it():
    ladder = ["vmtk", "snappy", "cfmesh"]
    ok = CR.with_conditions(MR.section("none"), {"request_txt": OK_INTERNAL, "intake_patches": PATCHES,
                                                 "purpose": "internal_cfd"})
    assert CR.engine_that_can(ok, "vmtk", ladder) == ("vmtk", "")
    res = CR.with_conditions(MR.section("none"), {"request_txt": RESOLVED_INTERNAL, "intake_patches": PATCHES,
                                                  "purpose": "internal_cfd"})
    assert CR.engine_that_can(res, "vmtk", ladder, conditions_y_plus=True)[0] == "snappy"


def test_external_flow_uses_the_body_length_and_the_flat_plate():
    ask = _ask(OK_EXTERNAL, "snappy", patches=[], purpose="external_cfd")
    assert ask.req["physics"]["flow"] == "external" and "hydraulic_diameter_m" not in ask.req["physics"]
    h, _m, how = CR.y_plus_height(50.0, ask.req["physics"], length_m=4.0, length_from="the body's length")
    re_n = 20.0 * 4.0 / FC.NU["air"]
    assert how["friction"] == "flat-plate skin friction" and how["laminar"] is False
    assert h == pytest.approx(2 * 50 * FC.NU["air"] / (20.0 * math.sqrt(CR.skin_friction(re_n) / 2)), rel=1e-9)


def test_a_resolved_wall_the_user_chose_is_built_on_a_laminar_flow_too():
    tiny = [{"name": "inlet", "type": "inlet", "diameter_mm": 1.0}, {"name": "outlet", "type": "outlet"}]
    ask = _ask(RESOLVED_INTERNAL, "snappy", patches=tiny)                # Re 1000: laminar, but the user chose it
    h = ask.first_layer()
    assert h is not None and not ask.laminar and ask.how["laminar"] is True
    assert h == pytest.approx(ask.how["visc_m"], rel=1e-9)                # y+ 1 at the top, by the turbulent estimate


def test_a_resolved_cfmesh_wall_grows_its_stack_to_the_core_never_thinner_than_asked():
    # CFDREQ-16: the plan's 2 layers kept the first layer and stepped from a 0.04 mm stack into 5 mm core cells (the
    # five cfMesh resolve parts at 84.7-88.7 degrees); the stack now fills cfMesh's depth from the asked first layer
    ask = _ask(RESOLVED_INTERNAL, "cfmesh")
    s = CR.cfmesh_strategy(ask, {"n_layers": 2}, length_m=1.0)
    h = s["first_layer_thickness"]
    wall = 200 * h
    out = CR.cfmesh_layer_fit(ask, s, wall)
    n, g = out["n_layers"], out["thickness_ratio"]
    assert g == pytest.approx(CR.CFMESH_RESOLVED_GROWTH) and 2 < n <= CR.MAX_BRIDGE_LAYERS
    stack = CR.CFMESH_STACK_REL * wall
    split = stack * (g - 1) / (g ** n - 1)            # the first layer cfMesh's split gives
    assert split >= h * (1 - 1e-9), "never thinner than asked"
    assert stack * (g - 1) / (g ** (n + 1) - 1) < h, "as many layers as the depth holds"
    assert out["first_layer_thickness"] == pytest.approx(h)
    # a first layer near the wall cell keeps the plan's count
    s2 = CR.cfmesh_strategy(_ask(RESOLVED_INTERNAL, "cfmesh"), {"n_layers": 2}, length_m=1.0)
    out2 = CR.cfmesh_layer_fit(_ask(RESOLVED_INTERNAL, "cfmesh"), s2, 3 * s2["first_layer_thickness"])
    assert out2["n_layers"] == 2


def test_a_passage_under_twelve_cells_across_keeps_the_plans_stack_and_says_why():
    # static_mixer_004: 4.0 wall cells across the narrowest 5%, 14 layers grown: 81 minutes of cartesianMesh, 179
    # degrees, no mesh - where its 2-layer mesh had delivered
    ask = _ask(RESOLVED_INTERNAL, "cfmesh")
    s = CR.cfmesh_strategy(ask, {"n_layers": 2}, length_m=1.0)
    wall = 200 * s["first_layer_thickness"]
    out = CR.cfmesh_layer_fit(ask, s, wall, cells_across=4.0)
    assert out["n_layers"] == 2
    assert "4.0 wall cells across" in ask.layer_note and "plan's 2 layers were kept" in ask.layer_note
    # at the floor and over it the stack grows as before
    ask2 = _ask(RESOLVED_INTERNAL, "cfmesh")
    s2 = CR.cfmesh_strategy(ask2, {"n_layers": 2}, length_m=1.0)
    assert CR.cfmesh_layer_fit(ask2, s2, wall, cells_across=float(CR.GROW_MIN_CELLS_ACROSS))["n_layers"] > 2
    ask3 = _ask(RESOLVED_INTERNAL, "cfmesh")
    s3 = CR.cfmesh_strategy(ask3, {"n_layers": 2}, length_m=1.0)
    assert CR.cfmesh_layer_fit(ask3, s3, wall, cells_across=None)["n_layers"] > 2


def test_a_first_cell_thinner_than_asked_is_told_with_the_built_number(tmp_path):
    # transition_005's rescue re-mesh: the first cell at 0.78 of the plan read as "at plan" inside the 1.5x band
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "meshDict").write_text(
        "boundaryLayers { patchBoundaryLayers { wall { nLayers 15; thicknessRatio 1.3; "
        "maxFirstLayerThickness 2.72113e-05; } } }\n")
    q = {"layer_coverage_pct": 100.0, "layer_coverage_basis": "height", "layer_first_planned_m": 2.72113e-05,
         "layer_area_at_plan_pct": 100.0, "layer_first_cell_m": 2.131e-05, "layer_first_vs_plan": 0.78}
    row = CR._first_layer_row(tmp_path, {"layers": {}}, q, {"first_m": 2.72113e-05})
    assert row is not None and "0.78 times that - thinner than asked" in row["delivered"]
    # at 0.9 of the plan it is the height asked
    q2 = dict(q, layer_first_cell_m=0.9 * 2.72113e-05, layer_first_vs_plan=0.9)
    assert "thinner" not in CR._first_layer_row(tmp_path, {"layers": {}}, q2, {"first_m": 2.72113e-05})["delivered"]
