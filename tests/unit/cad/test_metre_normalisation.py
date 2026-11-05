# Responsibility: Verify every surface format reaches its confirmed physical size, converted exactly once.
from __future__ import annotations

import pytest
from tests.cad_fixtures import SIDE, stl_extent, vtp_extent, write_stl, write_vtp

from meshpipeline.cad.normalise import AlreadyMetres, scale_polydata_file, scale_stl_file
from meshpipeline.contracts.coordinate_state import from_occ_transfer, from_source_file
from meshpipeline.contracts.geometry_units import (
    GeometryInterpretation,
    LengthUnit,
    ResolutionBasis,
    scale_to_metres,
)

#: The measured matrix: 10 source units under each supported confirmation.
EXPECTED_METRES = {
    LengthUnit.millimetre: 0.01,
    LengthUnit.centimetre: 0.10,
    LengthUnit.metre: 10.0,
    LengthUnit.inch: 0.254,
}


def _interp(unit: LengthUnit) -> GeometryInterpretation:
    return GeometryInterpretation("i-1", "owner-a", "src-1", unit,
                                  scale_to_metres(unit), ResolutionBasis.user_confirmed)


@pytest.mark.parametrize("unit,expected", list(EXPECTED_METRES.items()))
def test_stl_reaches_the_confirmed_physical_size(tmp_path, unit, expected):
    raw = write_stl(tmp_path / "raw.stl")
    assert stl_extent(raw) == pytest.approx(SIDE)          # raw coordinates are unit-free

    out = scale_stl_file(raw, tmp_path / f"prepared_{unit.value}.stl",
                         from_source_file(_interp(unit)))
    assert stl_extent(out) == pytest.approx(expected)


@pytest.mark.parametrize("unit,expected", list(EXPECTED_METRES.items()))
def test_vtp_reaches_the_confirmed_physical_size(tmp_path, unit, expected):
    raw = write_vtp(tmp_path / "raw.vtp")
    assert vtp_extent(raw) == pytest.approx(SIDE)

    out = scale_polydata_file(raw, tmp_path / f"prepared_{unit.value}.vtp",
                              from_source_file(_interp(unit)))
    assert vtp_extent(out) == pytest.approx(expected)


def test_vtp_keeps_topology_and_every_data_array(tmp_path):
    import pyvista as pv

    raw = write_vtp(tmp_path / "raw.vtp")
    before = pv.read(str(raw))
    out = scale_polydata_file(raw, tmp_path / "prepared.vtp",
                              from_source_file(_interp(LengthUnit.millimetre)))
    after = pv.read(str(out))

    assert after.n_points == before.n_points and after.n_cells == before.n_cells
    assert list(after.point_data["marker"]) == list(before.point_data["marker"])
    assert list(after.cell_data["region"]) == list(before.cell_data["region"])


def test_occ_output_converts_once_whatever_the_file_declared(tmp_path):
    from meshpipeline.cad.normalise import occ_scale_transform

    for declared in EXPECTED_METRES:
        trsf = occ_scale_transform(from_occ_transfer(_interp(declared)))
        # whatever the file said, the remaining conversion is OCC millimetres -> metres
        assert trsf.ScaleFactor() == pytest.approx(1e-3)


def test_metres_confirmed_on_an_stl_preserves_the_users_bytes(tmp_path):
    raw = write_stl(tmp_path / "raw.stl")
    out = scale_stl_file(raw, tmp_path / "prepared.stl",
                         from_source_file(_interp(LengthUnit.metre)))
    assert out.read_bytes() == raw.read_bytes()


def test_a_second_conversion_of_occ_output_is_refused():
    from meshpipeline.cad.normalise import occ_scale_transform

    broken = from_occ_transfer(_interp(LengthUnit.metre))
    object.__setattr__(broken, "current_unit", LengthUnit.metre)   # pretend it is already metres
    with pytest.raises(AlreadyMetres):
        occ_scale_transform(broken)
