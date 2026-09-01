# Responsibility: Verify the gmsh geometry report carries curve rows only for a planar (2D) model.
# A SOLID model binds groups onto surface tags; its curve table informed nothing and once bloated
# a 57-face volute's report past the builder's tool-output cap, starving the builder of every tag.
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytest.importorskip("gmsh", reason="gmsh SDK not installed")

REPO_DIR = Path(__file__).parent.parent.parent.parent
_SOLID_STEP = REPO_DIR / "tests" / "fixtures" / "geometry" / "cht_enclosing_2region.step"
_PLANAR_STEP = REPO_DIR / "tests" / "fixtures" / "geometry" / "plate_with_hole_2d.step"


def _report_for(fixture: Path, tmp_path: Path) -> dict:
    from meshpipeline.engines.gmsh import gmsh_runner
    ws = tmp_path / fixture.stem
    ws.mkdir()
    shutil.copy(fixture, ws / "geometry.step")
    return gmsh_runner.inspect_stl(ws)


def test_a_solid_model_reports_surfaces_but_no_curve_rows(tmp_path):
    report = _report_for(_SOLID_STEP, tmp_path)
    assert report["volumes"] >= 1
    assert report["surfaces"], "a solid model's surfaces are the binding vocabulary"
    assert report["curves"] == [], (
        "3D groups bind surface_tags; curve rows are dead weight that once pushed a real "
        "report past the tool cap")


def test_a_planar_model_still_reports_its_boundary_curves(tmp_path):
    report = _report_for(_PLANAR_STEP, tmp_path)
    assert report["volumes"] == 0
    assert report["curves"], (
        "a 2D planar case maps its contracted groups onto CURVE tags - the report must keep them")
    row = report["curves"][0]
    assert set(row) == {"tag", "length", "midpoint"}
