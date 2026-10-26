"""A flat plate must not be read as a car, and the unit question must not chase one.

Measured on the real file: the Corintis straight baseline's fluid domain is 32.5 x 19.9 x 0.8 in
its own units. Its length-to-width is 1.63, inside the car range, and the only height bound was an
upper one, so it classified as a car-like body. The unit was then chosen to make it a believable
car: millimetres were rejected for making it 3.25 cm, and INCHES were proposed - a 82.5 cm cold
plate, which no chip is.
"""
import pytest

from meshpipeline.contracts.geometry_units import LengthUnit
from meshpipeline.contracts.unit_plausibility import (
    CAR_HEIGHT_TO_WIDTH_MIN,
    GROUND_VEHICLE,
    proposed_unit,
    shape_kind,
)

#: The real part, in its own file units.
COLD_PLATE = (19.9, 32.5, 0.8)


def test_the_cold_plate_is_not_a_car():
    kind = shape_kind("external", COLD_PLATE, grounded=True, up_axis="+z")
    assert kind is not GROUND_VEHICLE, (
        "a plate 0.04 as tall as it is wide was read as a car-like body")


def test_a_real_car_still_is_one():
    # An Ahmed body: 1.044 x 0.389 x 0.288. The fix must not cost the case it was built for.
    assert shape_kind("external", (1.044, 0.389, 0.288), grounded=True,
                      up_axis="+z") is GROUND_VEHICLE
    # A saloon, roughly 4.7 x 1.9 x 1.45.
    assert shape_kind("external", (4.7, 1.9, 1.45), grounded=True,
                      up_axis="+z") is GROUND_VEHICLE


def test_the_lower_bound_sits_below_every_real_vehicle():
    """MEASURED ON THE FLAT END OF THE RANGE, which is what set this number.

    "A flatbed trailer is about 0.25" was a guess and it was wrong by three times. Measured: a
    flatbed deck is 0.115, a railway flatcar 0.133 and a lowboy deck 0.135, and at the first
    attempt's 0.15 all three fell past the vehicle reading into "a body in an external flow" -
    whose believable range is 0.005 to 5000 m, where nothing is ever implausible. A file with no
    declared unit then gets millimetres proposed, and a 12 m trailer is meshed as 12 mm.
    """
    for flattest in (0.115, 0.133, 0.135):
        assert CAR_HEIGHT_TO_WIDTH_MIN < flattest, "a real vehicle must stay a vehicle"
    # ...and still clear of the plate that started this, by nearly a factor of two
    plate = 0.8 / 19.9                       # 0.040
    assert plate < CAR_HEIGHT_TO_WIDTH_MIN
    assert CAR_HEIGHT_TO_WIDTH_MIN / plate > 1.9


@pytest.mark.parametrize("ratio,expect_car", [(0.04, False), (0.06, False),
                                              (0.115, True), (0.30, True), (0.75, True),
                                              (1.50, True)])
def test_the_boundary_behaves(ratio, expect_car):
    width = 19.9
    size = (width, width * 1.63, width * ratio)
    got = shape_kind("external", size, grounded=True, up_axis="+z") is GROUND_VEHICLE
    assert got is expect_car


def test_an_internal_cold_plate_was_always_fine():
    # Declared internal, the part is a flow passage and the car rule never ran. This pins that the
    # fix changes only the external reading.
    assert shape_kind("internal", COLD_PLATE) is not GROUND_VEHICLE


def test_millimetres_are_proposed_for_a_part_this_size():
    # 32.5 units. In millimetres that is 3.25 cm, which is a believable chip and was being
    # rejected only because a car cannot be 3.25 cm.
    assert proposed_unit(32.5) is LengthUnit.millimetre
