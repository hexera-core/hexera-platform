"""CFDREQ-24 / CFDREQ-25: an internal flow's wall-function layer and the cells round it.

CFDREQ-24: the cells at the wall (a thin feature's local refinement) suit the wall-function first layer, and the y+ row
reads a first cell at the planned height as there - and names a jump to much thinner cells above it.
CFDREQ-25: wall functions the user TYPED win over our minimum of cells across the passage (warned); wall functions only
ACCEPTED keep our minimum, and a wall resolved because they do not fit is the ask applied, said with why."""

from __future__ import annotations

import pytest
from tests.unit.engines.test_cfd_wall_treatment import OK_INTERNAL, PATCHES, SHM, _num
from tests.unit.engines.test_layer_census import _write_column_mesh
from tests.unit.engines.test_thin_plate_refinement import _plate

from meshpipeline.cad.thin_features import thin_refinement_boxes
from meshpipeline.contracts import flow_conditions as FC
from meshpipeline.engines import cfd_requirements as CR
from meshpipeline.engines import layer_census as LC
from meshpipeline.engines import mesh_requirements as MR


def _ask(*, typed: bool, engine: str = "snappy") -> CR.Ask:
    """Water at 20 C, 1 m/s through a part, wall functions: chosen by the user (`typed`) or the proposal accepted."""
    lead = f" {FC.ACCEPTED_LEAD} the fluid, its speed" + ("." if typed else ", the wall treatment.")
    text = ("Internal flow through the part, confirmed by the user on the geometry stage. "
            + FC.sentence("water at 20 °C", "1 m/s", "functions", internal=True, wall_said=typed) + lead)
    sec = CR.with_conditions(MR.section("none"), {"request_txt": text, "intake_patches": [],
                                                  "purpose": "internal_cfd"})
    return CR.Ask(MR.requirements_of(sec), engine)


# -- CFDREQ-25: typed vs accepted --------------------------------------------------------------------------------------

def test_the_lab_style_ask_is_the_accepted_proposal_and_a_chosen_one_is_typed():
    acc, typ = _ask(typed=False), _ask(typed=True)
    assert acc.treatment == typ.treatment == "functions"
    assert not CR.wall_functions_typed(acc) and CR.wall_functions_typed(typ)
    notes = _ask(typed=False)
    notes.treatment, notes.y_plus = None, 50.0                # a y+ of 30 or more typed in the notes
    assert CR.wall_functions_typed(notes)
    notes.y_plus = 1.0
    assert not CR.wall_functions_typed(notes)


def test_typed_wall_functions_take_fewer_cells_across_than_our_minimum_and_say_so():
    # an 18.6 mm bore (1.43 mm wall cells at our 13 across): a y+ 50 first layer (1.73 mm) cannot fit cfMesh's stack
    D, cap = 0.0186, CR.CFMESH_STACK_REL
    acc = _ask(typed=False, engine="cfmesh")
    assert CR.wall_function_bore_cells(acc, bore_D=D, cells_across=40, cap_rel=cap) == CR.WALL_FUNCTION_MIN_CELLS_ACROSS
    typ = _ask(typed=True, engine="cfmesh")
    n = CR.wall_function_bore_cells(typ, bore_D=D, cells_across=40, cap_rel=cap)
    assert CR.wall_function_hard_cells(cap) <= n < CR.WALL_FUNCTION_MIN_CELLS_ACROSS
    assert f"now has {n} cells across" in typ.wall_level_note and "normally keep at least 13" in typ.wall_level_note
    h = CR.y_plus_height(50.0, typ.req["physics"], length_m=D, length_from="the bore")[0]
    fit = CR.wall_fit(typ, h, D / n, cap_rel=cap)
    assert fit["treatment"] == "functions" and fit["y_plus"] >= 30.0          # wall functions, at y+ 30 or more
    assert CR.wall_fit(acc, h, D / 13, cap_rel=cap)["treatment"] == "resolved"


def test_the_hard_floor_keeps_the_first_cell_in_the_bores_log_layer():
    assert CR.wall_function_hard_cells(CR.FIRST_CAP_REL) == 6 and CR.wall_function_hard_cells(CR.CFMESH_STACK_REL) == 5
    # a 4 mm bore: even the thickest first cell its log layer holds (a fifth of the radius, 0.4 mm) sits under y+ 30
    typ = _ask(typed=True)
    assert CR.wall_function_bore_cells(typ, bore_D=0.004, cells_across=30) == 30
    h = CR.y_plus_height(50.0, typ.req["physics"], length_m=0.004, length_from="the bore")[0]
    assert CR.wall_fit(typ, h, 0.004 / 30)["treatment"] == "resolved"


def test_an_accepted_ask_resolved_for_the_passages_cells_is_applied_and_a_typed_one_is_the_nearest():
    D = 0.0186
    for typed, status in ((False, "applied"), (True, "nearest")):
        ask = _ask(typed=typed)
        h = CR.y_plus_height(50.0, ask.req["physics"], length_m=D, length_from="the bore")[0]
        fit = CR.wall_fit(ask, h, D / 40)
        assert fit["treatment"] == "resolved" and "do not suit this wall" in fit["why"]
        assert ("fallback" in fit) is (not typed)
        CR._say_wall(ask, fit, 9, 1.3)
        row = ask.rows[ask.rid("layers:y_plus")]
        assert row["status"] == status and "do not suit this wall" in row["built"]


def _case(tmp_path, wall_cell_of_h: float):
    """A snappyHexMesh case for the accepted proposal with wall cells `wall_cell_of_h` times the y+ 50 first layer."""
    sec = CR.with_conditions(MR.section("none"), {"request_txt": OK_INTERNAL, "intake_patches": PATCHES,
                                                  "purpose": "internal_cfd"})
    ask = CR.Ask(MR.requirements_of(sec), "snappy")
    h = CR.y_plus_height(50.0, ask.req["physics"])[0]
    out = CR.snappy_dict(ask, SHM, wall_cell_m=wall_cell_of_h * h)
    ws = tmp_path / "case"
    (ws / "system").mkdir(parents=True)
    (ws / "system" / "snappyHexMeshDict").write_text(out)
    MR.write(ws, sec)
    ask.write(ws)
    return ws, ask, float(_num(out, "firstLayerThickness")), h


def _y_row(rep: dict) -> dict:
    return next(r for r in rep["rows"] if r["field"] == "layers:y_plus")


def test_an_accepted_resolved_wall_reads_applied_with_why_not_missed(tmp_path):
    ws, ask, written, h = _case(tmp_path, 0.05)              # wall cells far finer than a y+ 30 cell
    assert ask.rows[ask.rid("layers:y_plus")]["status"] == "applied" and written < 0.05 * h
    q = {"cells": 400_000, "layer_coverage_pct": 95.0, "layer_coverage_basis": "height",
         "layer_first_planned_m": written, "layer_area_at_plan_pct": 95.0, "layer_first_cell_m": written,
         "layer_first_vs_plan": 1.0}
    row = _y_row(CR.report(ws, q, "snappy"))
    assert row["met"] is True and row["delivered"].startswith("a resolved wall")
    assert "do not suit this wall" in row["delivered"]
    q.update(layer_area_at_plan_pct=40.0, layer_coverage_pct=40.0)   # resolved layers lost are still a miss
    assert _y_row(CR.report(ws, q, "snappy"))["met"] is False


# -- CFDREQ-24: the layer and the cell above it ------------------------------------------------------------------------

def test_a_first_cell_at_plan_taller_than_its_faces_are_wide_is_there(tmp_path):
    pm = tmp_path / "smooth" / "polyMesh"
    _write_column_mesh(pm, [1.5, 1.4, 1.4, 1.4])          # unit-wide wall faces, a 1.5 layer, then like cells
    w = LC.census(pm, 1, ["wall"], planned_first_m=1.5)[0]["wall"]
    assert w["area_at_plan"] == 0.0                         # CFDREQ-21's count by shape is unchanged
    assert w["area_first_at_plan"] == 1.0 and w["area_under_jump"] == 0.0
    assert w["next_over_first"] == pytest.approx(1.4 / 1.5, rel=1e-3)
    hq = LC.height_quality(LC.census(pm, 1, ["wall"], planned_first_m=1.5)[0])
    assert hq["layer_area_first_at_plan_pct"] == 100.0 and hq["layer_area_under_jump_pct"] == 0.0


def test_a_first_cell_under_much_thinner_cells_is_a_jump(tmp_path):
    pm = tmp_path / "jump" / "polyMesh"
    _write_column_mesh(pm, [1.5, 0.27, 0.27, 0.27, 1.0])     # the measured 1 : 0.18 at the layer top
    w = LC.census(pm, 1, ["wall"], planned_first_m=1.5)[0]["wall"]
    assert w["area_first_at_plan"] == 0.0 and w["area_under_jump"] == 1.0
    assert w["next_over_first"] == pytest.approx(0.18, rel=1e-3)


def test_the_y_plus_row_names_the_jump_instead_of_no_layer(tmp_path):
    ws, _ask, written, _h = _case(tmp_path, 1.6)             # one layer, capped at 0.6 of the wall cell: y+ 48
    base = {"cells": 400_000, "layer_coverage_pct": 0.0, "layer_coverage_basis": "height",
            "layer_first_planned_m": written, "layer_area_at_plan_pct": 0.0, "layer_first_cell_m": written,
            "layer_first_vs_plan": 1.0}
    jump = _y_row(CR.report(ws, {**base, "layer_area_first_at_plan_pct": 0.0, "layer_area_under_jump_pct": 100.0,
                                 "layer_next_over_first": 0.18}, "snappy"))
    assert jump["met"] is False
    assert "on 100% it sits under much thinner cells" in jump["delivered"] and "5.6 times thinner" in jump["delivered"]
    smooth = _y_row(CR.report(ws, {**base, "layer_area_first_at_plan_pct": 100.0, "layer_area_under_jump_pct": 0.0,
                                   "layer_next_over_first": 0.9}, "snappy"))
    assert smooth["met"] is True and "a first cell of that height stands on 100% of the wall" in smooth["delivered"]
    old = _y_row(CR.report(ws, base, "snappy"))             # a record without the reading: as before
    assert old["met"] is False and "layers of that height stand on 0% of the wall" in old["delivered"]


# -- CFDREQ-24: a thin feature's cells under a wall-function layer -----------------------------------------------------

def test_a_thin_spot_is_refined_only_to_one_cell_across_when_two_would_squash_the_layer():
    # a 2.2 mm wall under a 3.42 mm wall cell and a 1.92 mm first layer: two across wanted 0.86 mm cells (+2)
    plain = thin_refinement_boxes(_plate(0.0022), cell_m=0.00342)
    assert plain and plain[0]["level_bump"] == 2 and plain.layer_note == ""
    held = thin_refinement_boxes(_plate(0.0022), cell_m=0.00342, keep_cell_m=0.00192 / CR.WALL_FUNCTION_JUMP)
    assert held and held[0]["level_bump"] == 1                  # 1.71 mm: one across, the plate still captured
    assert 0.00342 / 2 <= 0.0022 and "one across" in held.layer_note and "finer than" not in held.layer_note


def test_a_feature_thinner_than_the_layer_suits_keeps_the_cells_it_needs_and_says_so():
    held = thin_refinement_boxes(_plate(0.0006), cell_m=0.00342, keep_cell_m=0.00096)
    assert held and 0.00342 / 2 ** held[0]["level_bump"] <= 0.0006      # still captured
    assert "stands under thinner cells" in held.layer_note


def test_the_keep_cell_is_the_wall_function_layer_over_the_jump_allowed():
    acc = _ask(typed=False)
    D = 0.04447
    keep = CR.wall_function_keep_cell(acc, bore_D=D, wall_cell_m=0.00342)
    h = CR.y_plus_height(50.0, acc.req["physics"], length_m=D, length_from="the bore")[0]
    assert keep == pytest.approx(min(h, CR.FIRST_CAP_REL * 0.00342) / CR.FIRST_CAP_REL)     # CFDREQ-29
    # no wall-function layer to keep: a wall that will be resolved, or none asked
    assert CR.wall_function_keep_cell(acc, bore_D=D, wall_cell_m=0.0005) is None
    assert CR.wall_function_keep_cell(None, bore_D=D, wall_cell_m=0.00342) is None


# -- CFDREQ-29: no thinner cell above the wall-function layer -----------------------------------------------------------

def test_the_keep_cell_leaves_the_cell_above_the_layer_no_jump():
    # snappy pushes the wall into the cell the layer stands on: the cell above is about that cell less the layer, so a
    # cell of the keep size leaves it at least 1 / WALL_FUNCTION_JUMP of the layer (00157704: 1.57 mm on 1.31 mm cells)
    acc = _ask(typed=False)
    D, wall = 0.034, 0.002628
    keep = CR.wall_function_keep_cell(acc, bore_D=D, wall_cell_m=wall)
    layer = keep * CR.FIRST_CAP_REL
    assert keep <= wall * (1 + 1e-9)                         # never coarser than the wall cell the plan sized
    assert (keep - layer) / layer >= 1.0 / CR.WALL_FUNCTION_JUMP
    assert wall / 2 < keep                                   # the +1 level 00157704 took is under it


def test_a_thin_spot_no_reachable_level_captures_is_not_refined_under_a_wall_function_layer():
    # a 0.03 mm reading under 2.628 mm wall cells: even +4 (0.164 mm) is wider than it, so the refinement captures
    # nothing and only puts the 1.57 mm layer on cells it cannot stand on
    plate, cell, keep = _plate(0.00003), 0.002628, 0.001569 / CR.FIRST_CAP_REL
    plain = thin_refinement_boxes(plate, cell_m=cell)
    assert plain and plain[0]["level_bump"] >= 1             # no layer to keep: refined as before
    held = thin_refinement_boxes(plate, cell_m=cell, keep_cell_m=keep)
    assert not held                                          # no box: the wall keeps its cells
    assert held.thinnest_m == pytest.approx(0.00003, rel=0.2)
    assert "were not refined" in held.layer_note and "2.63 mm cells there" in held.layer_note
    assert "stands under thinner cells" not in held.layer_note


def test_a_thin_spot_the_levels_capture_is_still_refined_under_a_wall_function_layer():
    # CFDREQ-24's plate: 2.2 mm under 3.42 mm cells, +1 gives 1.71 mm - one across, captured - so it is kept
    held = thin_refinement_boxes(_plate(0.0022), cell_m=0.00342, keep_cell_m=0.00192 / CR.FIRST_CAP_REL)
    assert held and held[0]["level_bump"] == 1
    assert "were not refined" not in held.layer_note
