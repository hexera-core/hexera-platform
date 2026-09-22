# Responsibility: Verify STL/VTP repair inspection reports surface defects without changing bytes.
from __future__ import annotations

import pytest
from tests.cad_fixtures import write_stl, write_vtp

from meshpipeline.cad.repair.contracts import DefectCode
from meshpipeline.cad.repair.surface import inspect_surface_file


def _codes(report):
    return {d.code for d in report.defects}


def test_simple_stl_reports_measurements_and_no_defects(tmp_path):
    path = write_stl(tmp_path / "tri.stl")
    before = path.read_bytes()

    report = inspect_surface_file(path)

    assert path.read_bytes() == before
    assert report.summary == "No repair needed."
    assert _codes(report) == set()
    measurements = {m.name: m.value for m in report.measurements}
    assert measurements["n_triangles"] == 1
    assert measurements["n_points"] == 3
    assert measurements["boundary_edges"] == 3


def test_duplicate_stl_triangle_is_reported(tmp_path):
    path = tmp_path / "dup.stl"
    base = write_stl(tmp_path / "base.stl").read_text()
    tri = "\n".join(base.splitlines()[1:-1])
    path.write_text("solid dup\n" + tri + "\n" + tri + "\nendsolid dup\n")

    report = inspect_surface_file(path)

    assert DefectCode.duplicate_surface_data in _codes(report)
    defect = next(d for d in report.defects if d.code is DefectCode.duplicate_surface_data)
    assert defect.details["duplicate_faces"] == 1


def test_degenerate_stl_triangle_is_reported(tmp_path):
    path = tmp_path / "degenerate.stl"
    path.write_text(
        "solid bad\n"
        "facet normal 0 0 1\n  outer loop\n"
        "    vertex 0 0 0\n    vertex 0 0 0\n    vertex 1 0 0\n"
        "  endloop\nendfacet\nendsolid bad\n"
    )

    report = inspect_surface_file(path)

    assert DefectCode.degenerate_edge in _codes(report)


def test_vtp_surface_reports_measurements(tmp_path):
    pv = pytest.importorskip("pyvista")
    if getattr(pv, "PolyData", None) is object:
        pytest.skip("real pyvista unavailable")
    path = write_vtp(tmp_path / "tri.vtp")

    report = inspect_surface_file(path)

    measurements = {m.name: m.value for m in report.measurements}
    assert measurements["format"] == "vtp"
    assert measurements["n_triangles"] == 1
    assert measurements["n_points"] == 3
