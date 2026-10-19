# Responsibility: Verify the unit a file that states none is read in from what the part IS - its
# shape and flow (a car-like body on the ground, a thin flat body in the free stream, a passage) -
# with how sure that is and the other reading, and the concern a delivered mesh carries when it is
# no believable size for what it is.
# Boundaries: the pure contract. The numbers are dev-side corpus files (BlendedNet, AhmedML, the
# EPA tractor) and the DrivAer-sized car the e2e sweep meshed 4.7 mm long.
from __future__ import annotations

from meshpipeline.contracts.geometry_units import LengthUnit
from meshpipeline.contracts.unit_plausibility import (
    EXTERNAL_BODY,
    FLAT_LIFTING,
    FORMAT_UNIT,
    GROUND_VEHICLE,
    INTERNAL_PASSAGE,
    expected_from_words,
    infer_unit,
    scale_concern,
    shape_kind,
)

MM, M, IN, CM = LengthUnit.millimetre, LengthUnit.metre, LengthUnit.inch, LengthUnit.centimetre


def test_the_shape_and_the_flow_say_what_the_part_is():
    assert shape_kind("external", [4.7, 2.05, 1.4], grounded=True) is GROUND_VEHICLE
    assert shape_kind("external", [0.82, 0.30, 0.30], grounded=True) is GROUND_VEHICLE   # AhmedML
    assert shape_kind("external", [1.0, 1.0, 0.18]) is FLAT_LIFTING                      # BlendedNet
    assert shape_kind("internal", [400, 120, 120]) is INTERNAL_PASSAGE
    # a cube on the ground is no car, a slender rod is no wing, and up decides which side is tall
    assert shape_kind("external", [1.0, 1.0, 1.0], grounded=True) is EXTERNAL_BODY
    assert shape_kind("external", [1.0, 0.1, 0.1]) is EXTERNAL_BODY
    assert shape_kind("external", [4.7, 1.4, 2.05], grounded=True, up_axis="+y") is GROUND_VEHICLE
    assert shape_kind("", [1, 1, 1]) is None and shape_kind("external", [0, 0, 0]) is None


def test_a_4_7_unit_car_is_read_as_4_7_metres_and_said_so():
    r = infer_unit(4.7, shape=GROUND_VEHICLE)
    assert r.unit is M and r.confidence == "high"
    assert r.alternative is MM and abs(r.alternative_longest_m - 0.0047) < 1e-12
    assert r.words == ("This reads as a car-like body on the ground, 4.7 m long - metres? "
                       "(In millimetres it would be 4.7 mm long.)")
    # the same car drawn in millimetres stays millimetres - nothing else makes it a car
    r = infer_unit(4700.0, shape=GROUND_VEHICLE)
    assert r.unit is MM and r.confidence == "high" and r.alternative is None


def test_a_1_unit_blended_wing_and_a_1_unit_ahmed_body_are_metres():
    assert infer_unit(1.0, shape=FLAT_LIFTING).unit is M
    r = infer_unit(0.82, shape=GROUND_VEHICLE)
    assert r.unit is M and r.confidence == "high"


def test_a_shape_that_settles_nothing_keeps_millimetres_and_says_it_is_unsure():
    # a 400-unit duct: 400 mm, 4 m, 400 m are all passages - millimetres, low, the next reading named
    r = infer_unit(400.0, shape=INTERNAL_PASSAGE)
    assert r.unit is MM and r.confidence == "low" and r.alternative is CM
    # a 1.6-unit passage is no 1.6 mm pipe: the size floor speaks, and the reading stays unsure
    r = infer_unit(1.6, shape=INTERNAL_PASSAGE)
    assert r.unit is not MM and r.confidence == "low"
    # nothing known at all: the size alone, as before
    assert infer_unit(1.044).unit is M and infer_unit(1.044).confidence in ("low", "medium")


def test_the_users_words_speak_before_the_shape():
    r = infer_unit(229.1, expected=expected_from_words("wind around a city block of buildings"),
                   shape=EXTERNAL_BODY)
    assert r.unit is M


def test_a_gltf_file_reads_in_metres_only_where_the_part_is_recognised():
    gltf = FORMAT_UNIT["glb"]
    assert gltf is M
    assert infer_unit(10.9, shape=FLAT_LIFTING, format_unit=gltf).unit is M       # a Cessna, 10.9 m
    # a glTF copy of a CAD part in millimetres: a 1,054-unit body is not a 1 km body ...
    assert infer_unit(1054.0, shape=EXTERNAL_BODY, format_unit=gltf).unit is MM
    # ... and a 1,044-unit car is a 1.04 m car model, not a 10 m one in centimetres
    r = infer_unit(1044.0, shape=GROUND_VEHICLE, format_unit=gltf)
    assert r.unit is MM and r.alternative is M


def test_bad_sizes_infer_nothing():
    assert infer_unit(0.0) is None and infer_unit(float("nan")) is None
    assert infer_unit("x") is None                                               # type: ignore[arg-type]


def test_a_delivered_car_4_7_mm_long_carries_a_concern_naming_the_unit():
    c = scale_concern(0.0047, kind=GROUND_VEHICLE, unit_in_effect=MM)
    assert c is not None and c.too == "small" and c.other_unit is M
    assert c.words == ("This mesh is 4.7 mm long, which is far too small for a car-like body on "
                       "the ground. Check the unit of the file: in metres it would be 4.7 m long.")
    assert scale_concern(4.7, kind=GROUND_VEHICLE, unit_in_effect=M) is None
    # a 1:20 wind-tunnel model is unusual, not wrong: only a factor of three past the span speaks
    assert scale_concern(0.05, kind=GROUND_VEHICLE, unit_in_effect=M) is None
    assert scale_concern(0.0005, kind=INTERNAL_PASSAGE) is not None
    assert scale_concern(0.4, kind=INTERNAL_PASSAGE) is None
    assert scale_concern(0.001, kind=None) is None
