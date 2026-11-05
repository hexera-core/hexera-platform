# Responsibility: Verify the unit vocabulary is closed, its scales exact, and an interpretation immutable.
from __future__ import annotations

import pytest

from meshpipeline.contracts.geometry_units import (
    SCALE_TO_METRES,
    GeometryInterpretation,
    LengthUnit,
    ResolutionBasis,
    UnitResolutionError,
    parse_unit,
    scale_to_metres,
)


@pytest.mark.parametrize("answer,expected", [
    ("m", LengthUnit.metre), ("metres", LengthUnit.metre), ("Meter", LengthUnit.metre),
    ("mm", LengthUnit.millimetre), ("Millimetres", LengthUnit.millimetre),
    ("cm", LengthUnit.centimetre), ("centimeters", LengthUnit.centimetre),
    ("in", LengthUnit.inch), ("inches", LengthUnit.inch), ("  INCH  ", LengthUnit.inch),
])
def test_supported_answers_map_onto_the_closed_vocabulary(answer, expected):
    assert parse_unit(answer) is expected


@pytest.mark.parametrize("answer", ["", "   ", "furlongs", "mm or m", "about 10", "1e-3", None])
def test_unsupported_empty_or_contradictory_answers_are_refused(answer):
    with pytest.raises(UnitResolutionError):
        parse_unit(answer)


@pytest.mark.parametrize("unit,expected", [
    (LengthUnit.metre, 1.0), (LengthUnit.millimetre, 1e-3),
    (LengthUnit.centimetre, 1e-2), (LengthUnit.inch, 0.0254),
])
def test_scales_are_exact(unit, expected):
    assert scale_to_metres(unit) == expected


def test_the_inch_is_the_exact_definition():
    assert SCALE_TO_METRES[LengthUnit.inch] == 25.4 / 1000.0


def test_a_ten_unit_object_scales_as_the_user_confirmed():
    assert 10 * scale_to_metres(LengthUnit.millimetre) == pytest.approx(0.01)
    assert 10 * scale_to_metres(LengthUnit.metre) == pytest.approx(10.0)
    assert 10 * scale_to_metres(LengthUnit.inch) == pytest.approx(0.254)


def test_every_unit_has_a_scale():
    assert set(SCALE_TO_METRES) == set(LengthUnit)


def test_there_is_no_inferred_basis():
    assert {b.value for b in ResolutionBasis} == {"file_declared", "user_confirmed"}


def _interp(**over):
    base = {"interpretation_id": "i-1", "owner_id": "owner-a", "geometry_source_id": "src-1",
            "unit": LengthUnit.millimetre, "scale_to_metres": 1e-3,
            "basis": ResolutionBasis.user_confirmed}
    return GeometryInterpretation(**{**base, **over})


def test_an_interpretation_is_immutable():
    i = _interp()
    with pytest.raises(Exception):
        i.unit = LengthUnit.metre          # frozen dataclass


def test_identity_carries_physical_meaning_and_no_storage_detail():
    ident = _interp().identity()
    assert ident == {"geometry_source_id": "src-1", "unit": "mm",
                     "scale_to_metres": 1e-3, "basis": "user_confirmed"}
    blob = repr(ident)
    for private in ("/", "http", "bucket", "key", "sha256"):
        assert private not in blob


def test_different_units_give_different_identities():
    mm = _interp(unit=LengthUnit.millimetre, scale_to_metres=1e-3).identity()
    m = _interp(unit=LengthUnit.metre, scale_to_metres=1.0).identity()
    assert mm != m


def test_the_same_bytes_and_unit_give_a_stable_identity():
    assert _interp(interpretation_id="a").identity() == _interp(interpretation_id="b").identity()
