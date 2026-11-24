"""CFDREQ-19: an asked wall-function y+ is judged by the y+ the built mesh's wall cells give, not by a file's number."""

from __future__ import annotations

import pytest
from tests.unit.engines.test_cfd_wall_treatment import OK_INTERNAL, PATCHES, SHM, _num

from meshpipeline.engines import cfd_requirements as CR
from meshpipeline.engines import mesh_requirements as MR


def _case(tmp_path, wall_cell_of_h: float, *, engine: str = "snappy", typed: bool = False):
    """A snappyHexMesh case for water at 1 m/s with wall functions (y+ 50, the accepted proposal; `typed`: chosen by
    the user), wall cells `wall_cell_of_h` times the y+ 50 first layer: the dictionary and the plan as the driver
    leaves them."""
    sec = CR.with_conditions(MR.section("none"), {"request_txt": OK_INTERNAL, "intake_patches": PATCHES,
                                                  "purpose": "internal_cfd"})
    if typed:
        sec["requirements"]["layers"]["treatment_said"] = True
    ask = CR.Ask(MR.requirements_of(sec), engine)
    h = CR.y_plus_height(50.0, ask.req["physics"])[0]
    out = CR.snappy_dict(ask, SHM, wall_cell_m=wall_cell_of_h * h)
    ws = tmp_path / "case"
    (ws / "system").mkdir(parents=True)
    (ws / "system" / "snappyHexMeshDict").write_text(out)
    MR.write(ws, sec)
    ask.write(ws)
    return ws, ask, float(_num(out, "firstLayerThickness")), int(_num(out, "nSurfaceLayers")), h


def _q(written: float, first_cell: float, at_plan: float = 100.0) -> dict:
    # the quality record's census at the planned height (layer_census.height_quality)
    return {"cells": 400_000, "layer_coverage_pct": at_plan, "layer_coverage_basis": "height",
            "layer_first_planned_m": written, "layer_area_at_plan_pct": at_plan, "layer_first_cell_m": first_cell,
            "layer_first_vs_plan": first_cell / written}


def _y_row(rep: dict) -> dict:
    return next(r for r in rep["rows"] if r["field"] == "layers:y_plus")


def test_a_wall_function_layer_capped_by_its_wall_cell_is_met_at_the_y_plus_it_builds(tmp_path):
    # the CFD board's 00134075 / the CFDREQ-18 venturis: one layer, the first cell capped at 0.6 of the wall cell,
    # the wall's first cell measured at y+ 47-49 on all of the wall - read "missed" against the 50 asked
    ws, ask, written, n, h = _case(tmp_path, 1.6)
    assert n == 1 and written < h
    plan = ask.rows[ask.rid("layers:y_plus")]
    assert plan["status"] == "nearest" and plan["treatment"] == "functions"
    row = _y_row(CR.report(ws, _q(written, 0.99 * written), "snappy"))
    y = 50.0 * 0.99 * written / h
    assert 45 < y < 50
    assert row["met"] is True and row["excess"] == 0
    assert row["y_plus_measured"] == pytest.approx(y, rel=0.01)
    assert f"y+ about {MR._g(round(y))}" in row["delivered"] and "100% of the wall" in row["delivered"]


def test_a_layer_built_as_planned_is_met_by_its_measured_y_plus_too(tmp_path):
    ws, _ask, written, _n, h = _case(tmp_path, 10.0)
    assert written == pytest.approx(h, rel=1e-5)
    row = _y_row(CR.report(ws, _q(written, 1.05 * written), "snappy"))
    assert row["met"] is True and row["y_plus_measured"] == pytest.approx(52.5, rel=0.01)


def test_a_resolved_wall_against_a_typed_50_stays_missed_with_the_y_plus_reached_and_why(tmp_path):
    ws, _ask, written, _n, h = _case(tmp_path, 0.5, typed=True)
    row = _y_row(CR.report(ws, _q(written, written), "snappy"))
    assert row["met"] is False and row["excess"] >= 1
    assert row["y_plus_measured"] < 1.0
    assert "y+ about 0.5, not 50" in row["delivered"] and "wall functions do not suit" in row["delivered"]


def test_a_resolved_wall_for_an_accepted_50_is_the_proposal_applied_with_why(tmp_path):
    # CFDREQ-25: the accepted proposal says the wall is resolved where wall functions do not fit
    ws, _ask, written, _n, h = _case(tmp_path, 0.5)
    row = _y_row(CR.report(ws, _q(written, written), "snappy"))
    assert row["met"] is True and "wall functions do not suit" in row["delivered"]


def test_a_first_cell_outside_the_band_or_missing_on_most_of_the_wall_is_missed(tmp_path):
    ws, _ask, written, _n, h = _case(tmp_path, 10.0)
    # thinner than the band (0.6 N): y+ 25 is the buffer layer
    row = _y_row(CR.report(ws, _q(written, 0.5 * written), "snappy"))
    assert row["met"] is False and "y+ about 25, not 50" in row["delivered"]
    # thicker than 2 N
    assert _y_row(CR.report(ws, _q(written, 2.5 * written), "snappy"))["met"] is False
    # at the height on a fifth of the wall only
    row = _y_row(CR.report(ws, _q(written, written, at_plan=20.0), "snappy"))
    assert row["met"] is False and "20% of the wall" in row["delivered"]


def test_the_band_is_the_wall_function_range_and_close_to_the_ask():
    assert CR.y_plus_met(50.0, 47.0) and CR.y_plus_met(50.0, 31.0) and CR.y_plus_met(50.0, 99.0)
    assert not CR.y_plus_met(50.0, 29.0) and not CR.y_plus_met(50.0, 101.0) and not CR.y_plus_met(50.0, 0.5)
    assert CR.y_plus_met(200.0, 300.0) and not CR.y_plus_met(200.0, 301.0)
