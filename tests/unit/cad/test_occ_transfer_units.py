# Responsibility: Verify the STEP transfer normalises to the OCC system unit while IGES keeps the file's numbers.
from __future__ import annotations

import pytest

pytest.importorskip("OCP.STEPControl")

from tests.cad_fixtures import (  # noqa: E402
    SIDE,
    write_iges,
    write_step,
    write_step_of_units,
)

#: Declared unit -> the unit name, and what a box of SIDE units IN THAT UNIT measures in metres.
#: Used with `write_step_of_units`, which builds at that physical size - not with `write_step`,
#: whose boxes are all physically 10 mm however they are declared.
DECLARED = {
    "MM": ("millimetre", SIDE * 1e-3),
    "CM": ("centimetre", SIDE * 1e-2),
    "INCH": ("inch", SIDE * 0.0254),
    "M": ("metre", SIDE * 1.0),
}


def _transferred_extent(path) -> float:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.IFSelect import IFSelect_RetDone

    suffix = str(path).lower()
    if suffix.endswith((".igs", ".iges")):
        from OCP.IGESControl import IGESControl_Reader as Reader
    else:
        from OCP.STEPControl import STEPControl_Reader as Reader

    reader = Reader()
    assert reader.ReadFile(str(path)) == IFSelect_RetDone, f"OCC could not read {path}"
    reader.TransferRoots()
    shape = reader.OneShape()
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    x0, _y0, _z0, x1, _y1, _z1 = box.Get()
    return x1 - x0


@pytest.mark.parametrize("declared", list(DECLARED))
def test_the_step_transfer_normalises_to_the_occ_system_unit(declared, tmp_path):
    step = write_step(tmp_path / f"{declared}.step", declared)

    extent = _transferred_extent(step)

    # 1e-6 is a STEP round-trip tolerance, not a loose one: the smallest unit conversion in the
    # vocabulary is 25.4x, so any real conversion would miss this by seven orders of magnitude.
    assert extent == pytest.approx(SIDE, rel=1e-6), (
        f"OpenCASCADE returned {extent} for a {SIDE}-unit box declared {declared}. It used to "
        f"return {SIDE} for every unit, meaning the transfer preserved the file's numbers; a "
        "different answer means it now converts, and the staging boundary must change with it.")


def test_the_iges_transfer_preserves_the_files_numbers(tmp_path):
    iges = write_iges(tmp_path / "part.igs", "MM")

    assert _transferred_extent(iges) == pytest.approx(SIDE, rel=1e-6)


def test_the_files_really_hold_different_coordinates(tmp_path):
    import re as _re

    spans = {}
    for declared in DECLARED:
        txt = write_step(tmp_path / f"c{declared}.step", declared).read_text()
        xs = [float(m[0]) for m in
              _re.findall(r"CARTESIAN_POINT\('',\(([-\d.E+]+),([-\d.E+]+),([-\d.E+]+)\)\)", txt)]
        spans[declared] = max(xs) - min(xs)
    assert spans["MM"] == pytest.approx(10.0, rel=1e-6)
    assert spans["CM"] == pytest.approx(1.0, rel=1e-6)
    assert spans["INCH"] == pytest.approx(0.3937007874, rel=1e-6)
    assert spans["M"] == pytest.approx(0.01, rel=1e-6)


def test_the_declared_unit_is_really_in_the_file(tmp_path):
    step_mm = write_step(tmp_path / "mm.step", "MM").read_text()
    step_in = write_step(tmp_path / "in.step", "INCH").read_text()
    step_m = write_step(tmp_path / "m.step", "M").read_text()

    assert "SI_UNIT(.MILLI.,.METRE.)" in step_mm
    assert "CONVERSION_BASED_UNIT('INCH'" in step_in
    assert "SI_UNIT($,.METRE.)" in step_m


@pytest.mark.parametrize("declared,expected", [(d, v[1]) for d, v in DECLARED.items()])
def test_cad_reaches_metres_through_preparation(declared, expected, tmp_path):
    from tests._geometry_support import materialized
    from tests.unit.cad.test_surface_family_metres import _extent

    from meshpipeline.cad.staging import prepare_surface
    from meshpipeline.contracts.geometry_units import LengthUnit

    unit = {"MM": LengthUnit.millimetre, "CM": LengthUnit.centimetre,
            "INCH": LengthUnit.inch, "M": LengthUnit.metre}[declared]
    geom = materialized(tmp_path / "src", unit=unit, filename="part.step")
    write_step_of_units(geom.local_path, declared)

    surface = prepare_surface(geom, tmp_path / "ws" / "input.stl", engine="cfmesh")

    got = _extent(surface.path)
    assert got == pytest.approx([expected] * 3, rel=1e-6), (
        f"a {SIDE}-unit {declared} solid must prepare to {expected} m; got {got}")


def test_cad_is_not_scaled_twice(tmp_path):
    from tests._geometry_support import materialized
    from tests.unit.cad.test_surface_family_metres import _extent

    from meshpipeline.cad.staging import prepare_surface
    from meshpipeline.contracts.geometry_units import LengthUnit

    geom = materialized(tmp_path / "src", unit=LengthUnit.inch, filename="part.step")
    write_step_of_units(geom.local_path, "INCH")

    surface = prepare_surface(geom, tmp_path / "ws" / "input.stl", engine="cfmesh")

    expected = SIDE * 0.0254
    got = max(_extent(surface.path))
    assert got == pytest.approx(expected, rel=1e-6)
    assert got > expected * 0.5, (
        f"{got} is far below {expected} - the source unit looks to have been applied twice")
