# Responsibility: Verify which unit is proposed beside a part's size - the other reading when the
# unit in effect makes the part implausible ("117 mm" wind turbine blade, "3 km" pipe fitting, a
# triangle file whose numbers read as a 1 mm car), and none when the part is believable.
# Boundaries: the pure contract; the numbers are the real corpus files' longest sides.
from __future__ import annotations

import pytest

from meshpipeline.contracts.geometry_units import LengthUnit
from meshpipeline.contracts.unit_plausibility import (
    expected_from_estimate,
    expected_from_words,
    length_words,
    proposed_unit,
    suggest,
)

MM, M, IN, CM = LengthUnit.millimetre, LengthUnit.metre, LengthUnit.inch, LengthUnit.centimetre


def test_a_117_mm_wind_turbine_blade_is_offered_as_117_m():
    """The IEA 15 MW blade's STEP says millimetres; its numbers are metres. On size alone 117 mm
    is a believable pin - it is what the part is that makes it wrong."""
    words = expected_from_words("CFD around the IEA 15 MW wind turbine blade")
    s = suggest(117.0, MM, declared=True, expected=words)
    assert s is not None and s.unit is M and s.instead_of is MM
    assert s.words == "117 mm long - or 117 m if the file is in metres"
    assert s.why == "117 mm is small for a wind turbine blade"
    assert s.as_dict()["sizes"] == {"mm": "117 mm", "m": "117 m"}
    assert suggest(117.0, MM, declared=True) is None                 # size alone cannot tell
    # the naming model's own estimate says the same, and picks the closest reading
    est = expected_from_estimate("IEA 15 MW wind turbine blade", 117)
    assert suggest(117.0, MM, declared=True, expected=est).unit is M
    assert est.what == "an IEA 15 MW wind turbine blade"


def test_a_3_km_pipe_fitting_is_offered_as_3_m():
    assert suggest(3000.0, M, declared=True, expected=expected_from_words("a pipe fitting")).unit is MM
    s = suggest(3000.0, M, declared=True)                              # and wild enough on size alone
    assert s is not None and s.unit is MM and s.words == "3 km long - or 3 m if the file is in millimetres"


@pytest.mark.parametrize("longest,declared,unit", [
    (117.0, MM, None),          # a pin, as far as size goes
    (0.8164, M, None),          # the T106 cascade: 816 mm in metres
    (64593.0, MM, None),        # the CRM: 64.6 m
    (2635.5, IN, None),         # the CRM in inches
    (6.0, MM, None),            # a 6 mm KiCad button
    (0.5, MM, M),               # half a millimetre: wild
    (3000.0, M, MM),            # three kilometres: wild
])
def test_a_declared_unit_is_doubted_on_size_alone_only_when_wild(longest, declared, unit):
    s = suggest(longest, declared, declared=True)
    assert (s.unit if s else None) is unit


@pytest.mark.parametrize("longest,unit,words", [
    (1.044, M, ""),             # Ahmed body STL: a 1.04 m car, not a 1 mm one
    (2.1, M, ""),               # the OpenFOAM cyclone
    (2.043, M, ""),             # the motorbike
    (4.613, M, ""),             # the DrivAer body OBJ
    (0.052, M, ""),             # the flange: 52 mm
    (0.261, M, ""),             # the propeller OBJ
    (144.0, MM, ""),            # the pipe OBJ really is millimetres
    (229.1, MM, ""),            # buildings.obj on size alone reads as 229 mm...
    (229.1, M, "wind around a city block of buildings"),     # ...and as 229 m once the user says what it is
    (1048.8, MM, "water through this elbow"),                  # believable: millimetres
])
def test_a_file_that_declares_nothing_is_proposed_the_believable_reading(longest, unit, words):
    assert proposed_unit(longest, expected_from_words(words)) is unit


def test_what_the_part_is_can_also_confirm_the_unit_in_effect():
    assert suggest(1146.6, MM, declared=True, expected=expected_from_words("the ONERA M6 wing")) is None
    # two kinds named: the span of both, never narrower than either
    both = expected_from_words("a car, shaped like an aircraft")
    assert both.lo_m == 0.1 and both.hi_m == 100.0
    # a file that carries its wind tunnel: a 172 m box is no reason to doubt the metres it declares
    assert suggest(172.5, M, declared=True, expected=expected_from_words("the DrivAer car in its wind tunnel")) is None
    assert expected_from_words("nothing recognisable") is None


def test_no_reading_fits_leaves_the_size_rule_to_speak():
    # a "blood pump" housing read 3 m long: too big for either kind, and no unit makes it fit
    # (the FDA benchmark files are ten times too large in every unit we carry) - nothing proposed
    assert suggest(3079.1, MM, declared=True, expected=expected_from_words("the FDA blood pump housing")) is None


def test_bad_input_proposes_nothing():
    assert suggest(0.0, MM, declared=True) is None
    assert suggest(float("nan"), MM, declared=True) is None
    assert suggest("x", MM, declared=True) is None                     # type: ignore[arg-type]
    assert expected_from_estimate("a pipe", "not a number") is None
    assert expected_from_estimate("a pipe", -1) is None


def test_lengths_read_as_a_person_says_them():
    assert [length_words(x) for x in (0.117, 117.0, 3000.0, 0.0266, 1.05)] == ["11.7 cm", "117 m", "3 km", "2.66 cm", "1.05 m"]
