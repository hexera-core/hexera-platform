"""A car is read as a car, held to a real car's geometry rather than one built in the test.

UNTIL THIS FIXTURE EXISTED THERE WAS NO VEHICLE GEOMETRY IN THE REPOSITORY AT ALL, and that is
not a small gap: the car path is the half of the platform that has customers. The lab meshes EPA
tractors and trailers and 22 Ahmed bodies across ten formats, but that corpus lives on another
machine, so nothing a test could load existed and nothing in CI could notice a car being misread.

It got noticed the hard way. A change meant to find a cold plate's channels - a face with one hole
per channel reads as open rather than solid - turned a bluff body with a front grille and a rear
vent from "a solid body in external flow" into "a pipe wall with internal flow" at confidence 0.9,
and the stage would then have let the picture model call a car a cooled solid. Two things hid it:
every road-vehicle row in the lab table but one is an STL while that reading is B-rep only, so no
existing run could have changed; and there was nothing here to try.

ahmed_variant_001.step is the Ahmed reference body - 1044 x 389 x 288 mm, a 100 mm radius rounded
front and a 25 degree rear slant, one closed solid, millimetres declared in the file, 37 KB. It is
a row in the engine fitness table under its own name, so it is geometry the lab already meshes
rather than something invented here.
"""
from __future__ import annotations

from pathlib import Path

import pytest

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "geometry" / "ahmed_variant_001.step"

#: the Ahmed body's size, in metres
SIZE_M = (1.044, 0.389, 0.288)


@pytest.fixture(scope="module")
def car():
    from meshpipeline.cad.scout import scout_cad
    from meshpipeline.cad.unit_evidence import parser_applied_unit
    from meshpipeline.contracts.coordinate_state import from_occ_transfer
    from meshpipeline.contracts.geometry_units import (
        GeometryInterpretation,
        LengthUnit,
        ResolutionBasis,
        scale_to_metres,
    )
    if not FIXTURE.is_file():
        pytest.skip("the Ahmed fixture is not committed")
    interp = GeometryInterpretation(
        interpretation_id="t", owner_id="t", geometry_source_id="t",
        unit=LengthUnit.millimetre, scale_to_metres=scale_to_metres(LengthUnit.millimetre),
        basis=ResolutionBasis.file_declared, evidence="t")
    return scout_cad(FIXTURE, prepared=from_occ_transfer(interp, parser_applied_unit(FIXTURE)))


# #
# the file is what it says it is
# #
def test_it_is_one_closed_solid_in_millimetres(car):
    assert car.solids == 1
    assert car.size == pytest.approx(SIZE_M, abs=1e-4), (
        "the file declares millimetres, so 1044 x 389 x 288 must read as metres of that")


# #
# ...and the measuring step reads it as a body in a flow
# #
def test_the_fluid_goes_around_it_not_through_it(car):
    assert car.flow == "external"
    assert car.input_kind == "solid-body"
    assert car.body_kind == "single_solid"


def test_none_of_its_faces_is_read_as_perforated(car):
    # The Ahmed body has no grille, so this is the control for the rule rather than a test of it;
    # the grille cases are in test_perforated_face.py.
    assert all(not o.perforated for o in car.openings), [
        (o.kind, o.holes, o.perforated) for o in car.openings]


def test_the_stage_may_never_offer_it_as_a_cooled_solid(car):
    """The consequence that made a misreading dangerous rather than merely wrong: a car routed to
    the conjugate mesher waits for a coolant inlet it will never have."""
    from meshpipeline.application.geometry_check import _kinds_on_offer
    facts = {"flow": car.flow, "input_kind": car.input_kind, "body_kind": car.body_kind}
    proposal = {"openings": [{"id": i} for i in range(len(car.openings))]}
    assert "solid-assembly" not in _kinds_on_offer(facts, proposal)


# #
# ...and the size reader knows what a car is
# #
def test_its_size_is_believable_for_a_car(car):
    from meshpipeline.contracts.unit_plausibility import shape_kind
    kind = shape_kind(car.flow, tuple(car.size), grounded=True)
    said = getattr(kind, "what", "") or str(kind)
    assert "car" in said.lower() or "vehicle" in said.lower(), said


def test_it_sits_inside_the_band_that_tells_a_car_from_a_plate():
    """Both bounds, on this body's own numbers. There was only an upper one until a 32 x 20 x
    0.8 mm cold plate was read as a car and had millimetres rejected for making it 3.25 cm."""
    from meshpipeline.contracts.unit_plausibility import (
        CAR_HEIGHT_TO_WIDTH_MAX,
        CAR_HEIGHT_TO_WIDTH_MIN,
        CAR_LENGTH_TO_WIDTH,
    )
    length, width, height = SIZE_M
    assert height / width == pytest.approx(0.740, abs=0.001)
    assert CAR_HEIGHT_TO_WIDTH_MIN <= height / width <= CAR_HEIGHT_TO_WIDTH_MAX
    assert CAR_LENGTH_TO_WIDTH[0] <= length / width <= CAR_LENGTH_TO_WIDTH[1]
    # ...and a cold plate is nowhere near it
    assert 0.0008 / 0.0199 < CAR_HEIGHT_TO_WIDTH_MIN


# #
# ...and the words about a car still mean a car
# #
@pytest.mark.parametrize("said", [
    "drag on this car body",
    "external aero on a car at 30 m/s",
    "cd of this ahmed body",
])
def test_what_a_car_customer_types_still_reaches_cfd(said):
    from meshpipeline.application import purpose_map as pm
    r = pm.read_purpose(said)
    assert r.route == pm.ROUTE_FLOW, r.why
    assert not r.heat_in_solid
