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


# ------------------------------------------------------------- what the file's name says ----
def test_a_file_name_is_read_as_words():
    from meshpipeline.contracts.unit_plausibility import name_words

    assert name_words("IEA-15-240-RWT_blade.stl") == "IEA 15 240 RWT blade"
    assert name_words("AeratedStirredTank.obj.obj") == "Aerated Stirred Tank"
    assert name_words("9-588-304-LoD12-3D.obj") == "9 588 304 LoD12 3D"     # "LoD12" stays one word
    assert name_words(r"C:\parts\rotor_fan.stl") == "rotor fan"
    assert name_words("") == ""


def test_a_unit_written_in_the_file_name_is_read_but_not_a_dimension():
    from meshpipeline.contracts.unit_plausibility import unit_named_in

    assert unit_named_in("SU0248_mm.vtp") is MM
    assert unit_named_in("SU0248_mm_uncapped.vtp") is MM
    assert unit_named_in("SU0248_cm-trim.vtp") is CM
    assert unit_named_in("bracket-inches.stl") is IN and unit_named_in("hull_metres.stl") is M
    # a number before it makes it a dimension; "m" and "in" alone are too often something else
    assert unit_named_in("pipe_50_mm.stl") is None and unit_named_in("elbow 8 inch.stl") is None
    assert unit_named_in("model_m.stl") is None and unit_named_in("air_in.stl") is None
    # two different units named say nothing
    assert unit_named_in("adapter_mm_to_inch.stl") is None


def test_a_cyclone_named_by_its_file_is_metres_not_centimetres():
    # the OpenFOAM cyclone tutorial's surface, 2.1 units tall, internal: as a bare passage it read
    # as 2.1 cm (centimetres before metres for small passages, the arteries' rule); its file's name
    # says what it is, and no cyclone separator is 2 cm tall
    from meshpipeline.contracts.unit_plausibility import name_words

    bare = infer_unit(2.1, shape=INTERNAL_PASSAGE)
    assert bare.unit is CM and bare.confidence == "low"
    named = infer_unit(2.1, expected=expected_from_words(name_words("cyclone.stl")), shape=INTERNAL_PASSAGE)
    assert named.unit is M and named.confidence in ("medium", "high")


def test_a_unit_the_file_name_states_is_the_reading_when_the_part_is_believable_in_it():
    # the same pulmonary model shipped as SU0248_cm (12.9 units) and SU0248_mm (129 units)
    r = infer_unit(129.2, shape=INTERNAL_PASSAGE, named_unit=MM)
    assert r.unit is MM and r.confidence == "medium" and "name says millimetres" in r.why
    assert infer_unit(12.9, shape=INTERNAL_PASSAGE, named_unit=CM).unit is CM
    # a car named "_mm" that would be 4.7 mm long: the name and the size disagree - said, and low
    r = infer_unit(4.7, shape=GROUND_VEHICLE, named_unit=MM)
    assert r.unit is M and r.confidence == "low" and "name says millimetres" in r.why


def test_named_kinds_with_their_physical_sizes():
    from meshpipeline.contracts.unit_plausibility import name_words

    def read(raw, name, shape=EXTERNAL_BODY):
        return infer_unit(raw, expected=expected_from_words(name_words(name)), shape=shape)

    # a whole wind turbine (rotor, tower, monopile) stands about 290 m: metres, not 2.9 m in cm
    assert read(289.5, "IEA-15-240-RWT_full_monopile.stl").unit is M
    assert read(117.0, "IEA-15-240-RWT_blade.stl").unit is M
    # an aorta is 2-3.5 cm across, so a 22-unit aorta model is 22 cm, not 22 mm
    r = read(22.2, "0187_0002_aorta.vtp", INTERNAL_PASSAGE)
    assert r.unit is CM and r.confidence == "medium"           # 22 inches (56 cm) is one too
    # city-model exports name their buildings "bldg" and their level of detail "LoD2"/"LoD12"
    assert read(1075.0, "LoD2_32_356_5645_1_NW.obj").unit is M
    assert read(1072.0, "9-588-304-LoD12-3D.obj").unit is M
    assert read(1190.0, "53394525_bldg_6697_op_clean.stl").unit is M
    # a NACA section is an aerofoil whatever flow the shape was read with
    assert read(1.0, "NACA0012.obj", INTERNAL_PASSAGE).unit is M
