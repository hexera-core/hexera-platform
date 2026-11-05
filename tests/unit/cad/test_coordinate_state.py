# Responsibility: Verify raw-file and OCC-output coordinates use their own unit, so neither agrees by luck.
from __future__ import annotations

import pytest

from meshpipeline.cad.unit_evidence import OCC_OUTPUT_UNIT
from meshpipeline.contracts.coordinate_state import (
    CoordinateOrigin,
    from_occ_transfer,
    from_source_file,
)
from meshpipeline.contracts.geometry_units import (
    GeometryInterpretation,
    LengthUnit,
    ResolutionBasis,
    scale_to_metres,
)

SIDE = 10.0


def _interp(unit: LengthUnit) -> GeometryInterpretation:
    return GeometryInterpretation(
        interpretation_id="i-1", owner_id="owner-a", geometry_source_id="src-1",
        unit=unit, scale_to_metres=scale_to_metres(unit),
        basis=ResolutionBasis.user_confirmed)


@pytest.mark.parametrize("unit,expected_metres", [
    (LengthUnit.millimetre, 0.01),
    (LengthUnit.centimetre, 0.10),
    (LengthUnit.metre, 10.0),
    (LengthUnit.inch, 0.254),
])
def test_raw_file_coordinates_use_the_confirmed_unit(unit, expected_metres):
    prepared = from_source_file(_interp(unit))
    assert SIDE * prepared.to_metres == pytest.approx(expected_metres)
    assert not prepared.already_normalised


@pytest.mark.parametrize("declared", list(LengthUnit))
def test_occ_output_converts_from_occ_units_not_the_files(declared):
    prepared = from_occ_transfer(_interp(declared))
    assert prepared.current_unit is OCC_OUTPUT_UNIT
    assert SIDE * prepared.to_metres == pytest.approx(0.01)
    assert prepared.already_normalised


def test_the_two_boundaries_disagree_exactly_where_they_should():
    metres = _interp(LengthUnit.metre)
    assert SIDE * from_source_file(metres).to_metres == pytest.approx(10.0)
    assert SIDE * from_occ_transfer(metres).to_metres == pytest.approx(0.01)


def test_millimetres_agree_across_boundaries_which_is_why_the_old_bug_hid():
    mm = _interp(LengthUnit.millimetre)
    assert from_source_file(mm).to_metres == from_occ_transfer(mm).to_metres == 1e-3


def test_the_factor_is_derived_not_supplied():
    prepared = from_source_file(_interp(LengthUnit.inch))
    with pytest.raises(Exception):
        prepared.to_metres = 1.0            # derived property on a frozen dataclass


def test_prepared_coordinates_keep_their_provenance():
    prepared = from_occ_transfer(_interp(LengthUnit.inch))
    assert prepared.origin is CoordinateOrigin.occ_transfer
    assert prepared.interpretation.unit is LengthUnit.inch      # what the FILE said, preserved
    assert prepared.current_unit is LengthUnit.millimetre       # what the NUMBERS are now
