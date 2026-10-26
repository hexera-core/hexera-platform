"""A prefix decided the mesher, and whole millimetres described a microchannel as nothing.

TWO WORD-LEVEL DEFECTS, both found by reading what the patterns actually match rather than by a
report, and both measured before the fix:

    aerospace power module cold plate, coolant at 2 L/min   flow: aero
    aerospace cold plate thermal analysis                   flow: aero
    pin fin cold plate, I need the wake behind each pin     flow: wake
    externally finned cold plate, water cooling             flow: water
    external thermal resistance of this cold plate          ask domain, why "no cue"
    aerospace bracket, static stress                        ask domain, why "both: stress, aero"

`\\baero` is a PREFIX, so it matches AEROSPACE - a whole industry's name, present in half the
questions this platform's thermal customers ask. It sat in both the aerodynamics veto and the flow
word set, so an aerospace cold plate vetoed its own coolant on one side and voted for flow on the
other. The last line is the clearest: a bracket under static stress read as mechanics AND flow, on
a sentence with no fluid in it at all.

And the fifth is the worst of the six: the veto suppressed every cue in the sentence and the
reason string then said there were none.

THE SIZES. Every length in the opening roster printed with :.0f, so:

    a 2.0 x 0.25 mm port         "2 x 0 mm"
    a 0.25 mm microchannel       "0 x 0 mm"
    a 0.8 mm bore at (5, 0.4, 0.6)   "1 mm across at (5, 0, 1) mm"

The numbers behind it were always right. This is the text the owner confirms and the text the case
is read back out of, and a cold plate's channels live between 0.05 and 1 mm, where whole
millimetres cannot describe one at all.
"""
from __future__ import annotations

import pytest

from meshpipeline.application.geometry_confirmation import _mm
from meshpipeline.application.purpose_map import read_purpose


def _route(said: str) -> str:
    out = read_purpose(said)
    return out.route or f"ask:{out.ask}"


# #
# an industry's name is not a question about air flowing past a body
# #
@pytest.mark.parametrize("said", [
    "aerospace power module cold plate, coolant at 2 L/min",
    "aerospace cold plate thermal analysis",
    "pin fin cold plate, I need the wake behind each pin",
    "externally finned cold plate, water cooling",
    "external thermal resistance of this cold plate",
    "microchannel cold plate, water at 2 L/min",
    "pressure drop across the cold plate",
])
def test_a_cold_plate_keeps_reaching_cooling(said):
    assert _route(said) == "electronics", read_purpose(said)


def test_aerospace_is_not_aerodynamics():
    """It sat in the veto and in the flow words both, so it broke the reading twice."""
    assert _route("aerospace bracket, static stress") == "fea"
    assert "aero" not in read_purpose("aerospace bracket, static stress").why


@pytest.mark.parametrize("said", [
    "drag on this car body",
    "external aerodynamics of the Ahmed body",
    "aero map for this wing",
    "aerodynamic drag of the car",
    "lift and drag on a wing",
    "downforce on this cooling vent",
    "wake behind the truck",
    "external drag on an electronics enclosure",
])
def test_the_car_path_is_untouched(said):
    assert _route(said) == "flow", read_purpose(said)


def test_a_reason_never_contradicts_the_sentence_it_read():
    """The veto says so when it is why nothing matched, instead of reporting no cue on a sentence
    that carried two."""
    out = read_purpose("external pressure field around this heat sink casing")
    assert out.why != "no cue", out
    if out.ask:
        assert "flow past a body" in out.why, out


# #
# a length a cold plate can be described in
# #
@pytest.mark.parametrize(("value", "want"), [
    (0.25, "0.25"),           # a microchannel: "0" before
    (0.0457, "0.0457"),       # a 46 um channel
    (0.8, "0.8"),
    (2.0, "2"),               # no trailing zeros on a round number
    (10.58, "10.6"),
    (100.0, "100"),
    (1234.5, "1234"),         # never coarser than a whole millimetre
    (0.0, "0"),
])
def test_a_size_is_printed_to_three_significant_figures(value, want):
    assert _mm(value) == want


def test_a_microchannel_port_is_not_printed_as_nothing():
    assert f"{_mm(2.0)} x {_mm(0.25)} mm" == "2 x 0.25 mm"


def test_a_centroid_is_not_moved_by_the_rounding():
    """(5.0, 0.4, 0.6) printed as (5, 0, 1): 0.4 mm of movement on a part whose channel is 0.8."""
    assert [_mm(v) for v in (5.0, 0.4, 0.6)] == ["5", "0.4", "0.6"]


def test_a_size_that_is_not_a_number_is_not_a_crash():
    assert _mm(None) == "0" and _mm("") == "0"


@pytest.mark.parametrize(("value", "want"), [
    (2199.0, "2,199"),        # an annulus of flow: grouped, as it always was
    (1905.0, "1,905"),
    (0.19, "0.19"),           # a microchannel's cross-section: "0" before
    (21.72, "21.7"),
    (0.0, "0"),
])
def test_an_area_keeps_its_thousands_separator(value, want):
    """An area is not a length: thousands of square millimetres read as "2,199", and a fraction of
    one has to read as something other than nothing."""
    from meshpipeline.application.geometry_confirmation import _mm2
    assert _mm2(value) == want
