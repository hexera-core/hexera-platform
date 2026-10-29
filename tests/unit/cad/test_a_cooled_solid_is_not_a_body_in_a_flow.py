"""A solid the split cut coolant out of is a part the fluid goes THROUGH.

WHY THIS EXISTS, measured end to end on 2026-10-29. A 24 x 16 x 3 mm cold plate with two 1.2 mm
bores through its lid was uploaded to the running stack and its stage came back with:

    input_kind solid-body   flow external
    opening  inlet   1.35 mm across at (18, 13.9, 3) mm
    opening  outlet  1.35 mm across at (6, 2.05, 3) mm
    note: the part's faces gave 0 opening(s); its inside-space map found 2 mouth(s)
    note: the space inside the part is 415 mm3, open at 2 places

Two openings named inlet and outlet, their diameters and their positions, beside a reading that
says the fluid flows AROUND the part. Nothing asked the user which it was: `flow_question` returns
"" the moment there are two ports. Accepting what was drawn produced

    "No openings: the fluid flows around the whole body"

because `patches_from` returns no patches at all for external flow - and the intake then refused,
correctly, to mesh a conjugate case on a geometry with no coolant in it:

    "The confirmed geometry is a solid body for external flow with no coolant passage, so it does
     not contain the two regions required for chtMultiRegionFoam."

The scout is not wrong to read a plate as box-like (flat_share 1.32, fill 0.64; a 24 x 16 x 3 mm
block IS shaped like a body in a flow). What was wrong is serving that reading as the default
AFTER the split had cut 414.6 mm3 of coolant out of the part and found it open at both ends.

WHAT IS STILL NOT OVERRULED. `fold`'s rule for one solid - "THE SPLIT ADDS WHAT IT LEARNED AND
OVERRULES NOTHING" - is about the OPENINGS, which is what the pipe tee lost when the split
replaced its three correct ports with "the space inside the parts". Those stay the scout's. The
flow is a different question and the split has just answered it.
"""
import pytest

from meshpipeline.cad.fluid_volume_check import fold


def _answer(*, passages=None, found=True, **kw):
    """The split's answer as fluid_volume.Extraction.as_dict writes it."""
    out = {
        "found": found, "reason": "", "parts": 1, "caps": 6, "regions": 4, "seconds": 0.7,
        "pockets_left_out": 0, "leaks_mm": [],
        "passages": passages if passages is not None else [{
            "volume_l": 0.000414597, "volume_mm3": 414.5969, "seed_mm": [18.0, 13.95, 2.66],
            "openings": [
                {"id": 1, "proposed": True, "role": "inlet", "name": "inlet",
                 "centroid_mm": [18.0, 13.95, 3.0], "normal": [0.0, 0.0, 1.0],
                 "area_mm2": 1.44, "hole_mm2": 1.44, "wh_mm": [1.2, 1.2],
                 "on_extremity": True, "clear_ahead": True, "diameter_mm": 1.35},
                {"id": 2, "proposed": True, "role": "outlet", "name": "outlet",
                 "centroid_mm": [6.0, 2.05, 3.0], "normal": [0.0, 0.0, 1.0],
                 "area_mm2": 1.44, "hole_mm2": 1.44, "wh_mm": [1.2, 1.2],
                 "on_extremity": True, "clear_ahead": True, "diameter_mm": 1.35},
            ]}],
    }
    out.update(kw)
    return out


def _plate_facts(**kw):
    """The facts the scout hands over for that plate, exactly as it read them."""
    f = {"solids": 1, "body_kind": "single_solid", "input_kind": "solid-body", "flow": "external",
         "size_mm": [24.0, 16.0, 3.0], "notes": [], "confidence": {"input_kind": 0.7},
         "inside": {"rule": "INS-1", "has_inside": True, "passages": 1,
                    "mouths_per_passage": [2], "enclosed_mm3": 582.343},
         "openings": [{"id": 1, "name": "inlet", "role": "inlet", "area_mm2": 1.44},
                      {"id": 2, "name": "outlet", "role": "outlet", "area_mm2": 1.44}]}
    f.update(kw)
    return f


#   -- the reading ------------------------------------------------------------------------------

def test_the_plate_is_read_as_a_part_the_fluid_goes_through():
    f = _plate_facts()
    fold(f, _answer())
    assert f["flow"] == "internal"
    assert f["input_kind"] == "solid-assembly", (
        "purposes.py: solid-assembly is ONE solid with coolant passages through it")


def test_the_reading_is_said_to_be_the_measurement_s_and_not_a_guess():
    f = _plate_facts()
    fold(f, _answer())
    assert f["confidence"]["input_kind"] == 0.85 > 0.7, (
        "the split measured it; the scout's 0.7 was read off the part's proportions")


def test_the_volume_and_where_it_is_open_are_still_said():
    """The sentence a user reads, which is how they know the coolant was found at all."""
    f = _plate_facts()
    fold(f, _answer())
    assert any("open at 2 places" in n for n in f["notes"])
    assert any("415 mm3" in n for n in f["notes"])


def test_the_openings_are_still_the_scout_s_own():
    """THE PIPE TEE. It had a correct pipe_wall reading with its three ports and the split
    replaced them with "the space inside the parts". For one solid the scout has measured that
    solid's faces directly, so they stay its own and only the flow changes."""
    f = _plate_facts()
    before = [dict(o) for o in f["openings"]]
    fold(f, _answer())
    assert f["openings"] == before
    assert "openings_from" not in f, "that key is the assembly path's, not this one's"
    assert f.get("seed_point_mm") is None, "the top-level seed stays the scout's too"


def test_the_coolant_and_its_seed_still_travel_where_a_conjugate_case_reads_them():
    f = _plate_facts()
    fold(f, _answer())
    assert f["fluid_volume"]["found"] is True
    assert f["fluid_volume"]["passages"][0]["seed_mm"] == [18.0, 13.95, 2.66]
    assert f["fluid_volume"]["passages"][0]["volume_mm3"] == pytest.approx(414.5969)


#   -- what must not change ---------------------------------------------------------------------

def test_a_solid_with_no_coolant_in_it_is_left_exactly_as_it_was():
    """Having no cavity is the normal state of a solid body. A car, a bracket, a hull."""
    f = _plate_facts(inside={"rule": "INS-1", "has_inside": False, "mouths_per_passage": []},
                     openings=[])
    fold(f, _answer(found=False, reason="no space inside the parts", passages=[]))
    assert (f["flow"], f["input_kind"]) == ("external", "solid-body")


def test_one_opening_is_no_flow_through_and_changes_nothing():
    """A passage needs a way in AND a way out; one hole is a bore or a bolt hole."""
    f = _plate_facts()
    one = _answer(passages=[{
        "volume_l": 1e-6, "volume_mm3": 1.0, "seed_mm": [1.0, 1.0, 1.0],
        "openings": [{"id": 1, "proposed": True, "role": "inlet", "name": "inlet",
                      "centroid_mm": [18.0, 13.95, 3.0], "normal": [0.0, 0.0, 1.0],
                      "area_mm2": 1.44, "hole_mm2": 1.44, "wh_mm": [1.2, 1.2],
                      "on_extremity": True, "clear_ahead": True}]}])
    fold(f, one)
    assert (f["flow"], f["input_kind"]) == ("external", "solid-body")


def test_a_split_that_did_not_finish_says_nothing_about_the_part():
    """A crash or a timeout is not the geometry's fault, and must not be read as a reading."""
    f = _plate_facts()
    fold(f, _answer(found=False, error="timed out after 20s", passages=[]))
    assert (f["flow"], f["input_kind"]) == ("external", "solid-body")
    assert f["fluid_volume"]["unfinished"] is True


def test_an_assembly_still_takes_the_split_s_own_openings():
    """Several solids is the other path entirely: the scout sees dozens of flat faces spread over
    several parts and cannot tell which bound the fluid, so there the split does replace them."""
    f = _plate_facts(solids=4)
    fold(f, _answer(parts=4))
    assert f["openings_from"] == "fluid_volume"
    assert f["seed_point_mm"] == [18.0, 13.95, 2.66]


def test_a_pipe_wall_was_already_internal_so_nothing_moves_for_it():
    f = _plate_facts(flow="internal", input_kind="body-surface", body_kind="pipe_wall")
    fold(f, _answer())
    assert f["flow"] == "internal"
    assert f["body_kind"] == "pipe_wall", "the body kind is not this change's to touch"
