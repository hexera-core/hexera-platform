# Responsibility: Verify that a STEP file in a well-formed unit the platform has no name for (a foot) is staged at the size its own declaration gives - the conversion OpenCASCADE applies is never undone as if it were metres.
# Boundaries: a real OCC transfer of a written STEP file; no mesher.
"""A FOOT IS 0.3048 M (FEA hunt, 27 Oct).

An AutoCAD export declared CONVERSION_BASED_UNIT('FOOT', 304.8 mm). OpenCASCADE converted it as it read; the
platform called the unit unknown, assumed millimetres and took OCC's conversion for a metre one, so the
coordinate state divided it back out: the assembly staged at 1/305 of what OCC had read.
"""
from __future__ import annotations

import pytest

pytest.importorskip("OCP.BRepPrimAPI")

from meshpipeline.cad.scout import read_cad  # noqa: E402
from meshpipeline.cad.unit_evidence import parser_applied_unit, read_declared_unit  # noqa: E402
from meshpipeline.contracts.coordinate_state import from_occ_transfer  # noqa: E402
from meshpipeline.contracts.geometry_units import (  # noqa: E402
    GeometryInterpretation,
    LengthUnit,
    ResolutionBasis,
    scale_to_metres,
)


def _cube_in(tmp_path, unit: str):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    # the writer first: OCC registers "write.step.unit" when the first STEP writer of the process is made, and a unit
    # set before that is dropped (the file then says millimetres: this test failed whenever it ran first)
    w = STEPControl_Writer()
    Interface_Static.SetCVal_s("write.step.unit", unit)
    try:
        # a cube 1 file unit along each side: OCC writes its millimetres in the file's unit
        side_mm = {"FT": 304.8, "MM": 1.0}[unit]
        w.Transfer(BRepPrimAPI_MakeBox(side_mm, side_mm, side_mm).Shape(), STEPControl_AsIs)
        path = tmp_path / f"cube_{unit}.step"
        w.Write(str(path))
    finally:
        Interface_Static.SetCVal_s("write.step.unit", "MM")
    return path


def _staged_side_m(path) -> float:
    ev = read_declared_unit(path)
    assert ev.resolved
    interp = GeometryInterpretation(interpretation_id="i", owner_id="o", geometry_source_id="s", unit=ev.unit,
                                    scale_to_metres=scale_to_metres(ev.unit), basis=ResolutionBasis.file_declared)
    prepared = from_occ_transfer(interp, parser_applied_unit(path))
    from meshpipeline.cad import fea_assembly as A

    lo, hi = A._box(read_cad(path))
    return float((hi - lo).max()) * prepared.to_metres


def test_a_file_in_feet_stages_at_its_declared_size(tmp_path):
    path = _cube_in(tmp_path, "FT")
    assert "FOOT" in path.read_text().upper()
    assert read_declared_unit(path).unit is LengthUnit.millimetre
    assert _staged_side_m(path) == pytest.approx(0.3048, rel=1e-6)


def test_a_file_in_millimetres_is_read_as_before(tmp_path):
    assert _staged_side_m(_cube_in(tmp_path, "MM")) == pytest.approx(0.001, rel=1e-6)


def _iges_cube_in(tmp_path, unit: str):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.IGESControl import IGESControl_Writer
    from OCP.STEPControl import STEPControl_Writer

    # (OCC: an IGES writer made before any STEP writer leaves "write.step.unit" unregistered, and the STEP tests of
    # this file - in whatever order they run - would then write millimetres)
    STEPControl_Writer()
    side_mm = {"FT": 304.8, "MM": 1.0, "IN": 25.4}[unit]
    w = IGESControl_Writer(unit, 1)
    w.AddShape(BRepPrimAPI_MakeBox(side_mm, side_mm, side_mm).Shape())
    w.ComputeModel()
    path = tmp_path / f"cube_{unit}.igs"
    assert w.Write(str(path))
    return path


@pytest.mark.parametrize("unit,side_m", [("FT", 0.3048), ("MM", 0.001), ("IN", 0.0254)])
def test_an_iges_file_stages_at_its_declared_size(tmp_path, unit, side_m):
    """UNIT-IGES-1: an IGES file in feet (unit flag 4, "FT": a Maxsurf hull export) is read at the size its own
    flag gives. Before, the unit was "unsupported", millimetres were assumed and OCC's conversion was undone as if
    it were metres: the 2 m hull staged at 2 mm."""
    path = _iges_cube_in(tmp_path, unit)
    assert _staged_side_m(path) == pytest.approx(side_m, rel=1e-6)
