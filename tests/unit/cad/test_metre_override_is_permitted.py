# Responsibility: Verify a confirmed unit is honoured, and a post-transfer state cannot claim a different one.
from __future__ import annotations

import pytest

from meshpipeline.cad.normalise import AlreadyMetres, occ_scale_transform
from meshpipeline.contracts.coordinate_state import (
    OCC_OUTPUT_UNIT,
    CoordinateOrigin,
    CoordinateStateError,
    PreparedCoordinates,
    from_occ_transfer,
)
from meshpipeline.contracts.geometry_units import (
    GeometryInterpretation,
    LengthUnit,
    ResolutionBasis,
    scale_to_metres,
)

UNITS = [LengthUnit.metre, LengthUnit.millimetre, LengthUnit.centimetre, LengthUnit.inch]


def _interp(unit: LengthUnit, basis=ResolutionBasis.user_confirmed) -> GeometryInterpretation:
    return GeometryInterpretation(
        interpretation_id="00000000-0000-4000-8000-000000000001", owner_id="owner-1",
        geometry_source_id="00000000-0000-4000-8000-000000000002", unit=unit,
        scale_to_metres=scale_to_metres(unit), basis=basis, evidence="probe")


@pytest.mark.parametrize("declared", UNITS)
@pytest.mark.parametrize("confirmed", UNITS)
def test_every_declared_confirmed_pair_yields_the_confirmed_reading(declared, confirmed):
    state = from_occ_transfer(_interp(confirmed), declared)
    occ_mm_per_raw = scale_to_metres(declared) / scale_to_metres(LengthUnit.millimetre)
    expected = scale_to_metres(confirmed) / occ_mm_per_raw
    assert state.to_metres == pytest.approx(expected, rel=1e-12)


def test_the_millimetre_to_metre_override_is_exactly_one_and_permitted():
    state = from_occ_transfer(_interp(LengthUnit.metre), LengthUnit.millimetre)
    assert state.to_metres == 1.0
    trsf = occ_scale_transform(state)                       # must not raise
    assert trsf.ScaleFactor() == pytest.approx(1.0)


def test_a_trusted_declaration_is_unaffected():
    state = from_occ_transfer(_interp(LengthUnit.millimetre, ResolutionBasis.file_declared),
                              LengthUnit.millimetre)
    assert state.to_metres == pytest.approx(1e-3)


def test_a_post_transfer_state_claiming_another_unit_cannot_be_built():
    with pytest.raises(CoordinateStateError, match="post-transfer coordinates are mm"):
        PreparedCoordinates(CoordinateOrigin.occ_transfer, LengthUnit.metre,
                            _interp(LengthUnit.metre))


def test_a_rewritten_state_is_still_refused_at_the_transform():
    state = from_occ_transfer(_interp(LengthUnit.metre), LengthUnit.millimetre)
    object.__setattr__(state, "current_unit", LengthUnit.metre)
    with pytest.raises(AlreadyMetres):
        occ_scale_transform(state)


def test_the_occ_output_unit_is_what_the_contract_says_it_is():
    assert OCC_OUTPUT_UNIT is LengthUnit.millimetre
    assert from_occ_transfer(_interp(LengthUnit.inch)).current_unit is OCC_OUTPUT_UNIT
