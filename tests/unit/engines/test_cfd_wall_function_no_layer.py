"""CFDREQ-24 (the jump) and CFDREQ-26: a wall-function layer never counts under a jump; where it would crush the cells
above it and the wall's own cells already sit at the y+ asked, the wall has no layer and is judged by its own cells; a
thin spot held back for the layer keeps its cells where a fluid passage there needs them."""

from __future__ import annotations

import numpy as np
import pytest
from tests.unit.engines.test_cfd_wall_function_cells import _ask
from tests.unit.engines.test_layer_census import _write_column_mesh
from tests.unit.engines.test_thin_plate_refinement import _plate

from meshpipeline.cad.thin_features import thin_refinement_boxes
from meshpipeline.engines import cfd_requirements as CR
from meshpipeline.engines import layer_census as LC
from meshpipeline.engines import verdict_limits as VL

D, CELL = 0.04447, 0.00342


# -- CFDREQ-24: a flat layer at plan under a jump is not the wall-function cell ----------------------------------------

def test_a_flat_layer_at_plan_under_a_jump_leaves_the_y_plus_share(tmp_path):
    pm = tmp_path / "flat" / "polyMesh"
    _write_column_mesh(pm, [0.5, 0.1, 0.1, 1.0])              # a flat layer at plan, the cell above 5x thinner
    w = LC.census(pm, 1, ["wall"], planned_first_m=0.5)[0]["wall"]
    assert w["area_at_plan"] == 1.0                           # CFDREQ-21 still counts the layer by shape
    assert w["area_under_jump"] == 1.0 and w["area_first_at_plan"] == 0.0
    smooth = tmp_path / "smooth" / "polyMesh"
    _write_column_mesh(smooth, [0.5, 0.45, 0.45, 1.0])
    assert LC.census(smooth, 1, ["wall"], planned_first_m=0.5)[0]["wall"]["area_first_at_plan"] == 1.0


def test_with_a_band_the_wall_cell_itself_is_judged(tmp_path):
    pm = tmp_path / "cells" / "polyMesh"
    _write_column_mesh(pm, [3.0, 3.0, 3.0])                   # no layer: the wall's own cells
    assert LC.census(pm, 1, ["wall"], planned_first_m=1.5)[0]["wall"]["area_first_at_plan"] == 0.0
    w = LC.census(pm, 1, ["wall"], planned_first_m=1.5, band_m=(1.0, 4.0))[0]["wall"]
    assert w["area_first_at_plan"] == 1.0 and w["area_under_jump"] == 0.0
    assert LC.census(pm, 1, ["wall"], planned_first_m=1.5, band_m=(1.0, 2.0))[0]["wall"]["area_first_at_plan"] == 0.0


# -- CFDREQ-26: no layer where it would crush and the wall cells already give the y+ ----------------------------------

def _box(tris, bump: int) -> dict:
    T = np.asarray(tris)
    return {"min": (T.reshape(-1, 3).min(axis=0) - 1e-6).tolist(),
            "max": (T.reshape(-1, 3).max(axis=0) + 1e-6).tolist(), "level_bump": bump}


def test_a_wall_refined_under_the_layer_has_no_layer_when_its_cells_give_the_y_plus():
    acc, tris = _ask(typed=False), _plate(0.0022)
    keep = CR.wall_function_keep_cell(acc, bore_D=D, wall_cell_m=CELL)
    plain = CR.wall_function_cell_plan(acc, bore_D=D, wall_cell_m=CELL, tris=tris, boxes=[])
    assert plain is not None and plain["no_layer"] is False and plain["crushed_pct"] == 0.0
    assert plain["layer_m"] == pytest.approx(keep * CR.WALL_FUNCTION_JUMP, rel=1e-3)
    out = CR.wall_function_cell_plan(acc, bore_D=D, wall_cell_m=CELL, tris=tris, boxes=[_box(tris, 1)])
    _h, _m, how = CR.y_plus_height(50.0, acc.req["physics"], length_m=D, length_from="the bore")
    y_fine = CR.y_plus_of(CELL / 2, how, 50.0)
    assert CELL / 2 < CR.WALL_FUNCTION_CRUSH * out["layer_m"] and out["crushed_pct"] == 100.0
    in_band = out["y_lo"] <= y_fine <= out["y_hi"]
    assert out["no_layer"] is in_band and out["with_layer_pct"] == 0.0
    if in_band:
        assert out["words"].startswith("no wall layer") and "crush" in out["words"]


def test_cells_under_the_band_keep_the_layer_plan_and_say_why():
    acc, tris = _ask(typed=False), _plate(0.0006)
    out = CR.wall_function_cell_plan(acc, bore_D=D, wall_cell_m=CELL, tris=tris, boxes=[_box(tris, 3)])
    assert out["under_pct"] == 100.0 and out["no_layer_pct"] == 0.0 and out["no_layer"] is False


def test_no_plan_without_a_wall_function_layer():
    acc = _ask(typed=False)
    assert CR.wall_function_cell_plan(acc, bore_D=D, wall_cell_m=0.0005, tris=_plate(0.0022), boxes=[]) is None
    assert CR.wall_function_cell_plan(None, bore_D=D, wall_cell_m=CELL, tris=_plate(0.0022), boxes=[]) is None


def test_the_y_plus_row_of_a_wall_with_no_layer_reads_its_own_cells(monkeypatch):
    p = {"built": "no wall layer: the wall's own cells already sit at y+ 30 to 100", "wall_cell_first": True}
    seen = {}

    def fake(_ws, _q, planned, *, band_m=None):
        seen["band"] = band_m
        return {"layer_area_first_at_plan_pct": 86.0, "layer_first_cell_m": 0.0025,
                "layer_area_under_jump_pct": 3.0, "layer_next_over_first": 0.9}

    monkeypatch.setattr(CR, "built_first_cells", fake)
    row = CR._wall_cell_y_row(None, {}, p, 50.0, 0.00192, 47.0)
    assert seen["band"] == pytest.approx((0.00192 * 30 / 47, 0.00192 * 100 / 47))
    assert row["met"] is True and row["delivered"].startswith("no wall layer")
    assert "stands on 86% of the wall" in row["delivered"] and "3%" in row["delivered"]
    monkeypatch.setattr(CR, "built_first_cells", lambda *_a, **_k: {"layer_area_first_at_plan_pct": 40.0,
                                                                     "layer_first_cell_m": 0.0025})
    low = CR._wall_cell_y_row(None, {}, p, 50.0, 0.00192, 47.0)
    assert low["met"] is False and "outside that band" in low["delivered"]


def test_the_coverage_row_says_no_layer_was_planned():
    rows = VL.inputs_for(engine="snappy", topology="internal", manifest={"quality": {"layers_left_out": "no wall layer"}},
                         valid=False, layers_planned=True)["limits"]
    cov = next(r for r in rows if r["key"] == "layer_coverage_pct")
    assert cov["measured"] is None and "No wall layer was planned" in cov["said"]


# -- CFDREQ-26: a fluid passage at a thin spot keeps its cells --------------------------------------------------------

def test_a_held_thin_spot_keeps_its_cells_where_a_fluid_passage_needs_them():
    tris = _plate(0.0022)
    keep = 0.00192 / CR.WALL_FUNCTION_JUMP
    pts = np.asarray(tris).reshape(-1, 3)
    held = thin_refinement_boxes(tris, cell_m=CELL, keep_cell_m=keep)
    assert held[0]["level_bump"] == 1
    narrow = thin_refinement_boxes(tris, cell_m=CELL, keep_cell_m=keep,
                                   passage=(pts, np.full(len(pts), 0.0058)), passage_cells=12)
    assert narrow[0]["level_bump"] == 2 and "fluid passage" in narrow.layer_note
    wide = thin_refinement_boxes(tris, cell_m=CELL, keep_cell_m=keep,
                                 passage=(pts, np.full(len(pts), 0.02)), passage_cells=12)
    assert wide[0]["level_bump"] == 1 and "fluid passage" not in wide.layer_note
    # with no hold-back the passage changes nothing
    assert thin_refinement_boxes(tris, cell_m=CELL, passage=(pts, np.full(len(pts), 0.0058)))[0]["level_bump"] == 2
