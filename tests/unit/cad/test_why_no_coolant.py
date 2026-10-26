"""When the coolant cannot be found, say which of the reasons it is.

Five of the eleven corpus parts refuse, for FOUR different reasons, and every one of them used to
arrive at the stage with no coolant and no reason at all. That is not a neutral silence: the first
option on the stage reads "the part's wall, hollow inside for the fluid", which describes a cold
plate exactly and means the metal is thrown away, so a silent refusal steers the customer into
losing half their part.

MEASURED, with the sentence each part now gets:

    corintis baseline       no hole was found in this plate's faces (the split never ran)
    tuckerman_pease_81F9    only one hole was found (the split never ran)
    lee_garimella_test1     no hole in any face for it to enter by
    pin_fin_300um           526 possible openings and the space still not closed off
    murray_branching_4gen   the space inside the part is sealed

Three of those are the same underlying thing - channels open at the top, so the mouths are notches
in the part's outline rather than holes in a face, and only a hole can be capped. They are also
the three most-cited microchannel cold plates in the literature, which is why saying something
useful matters more here than anywhere else. Capping an open groove is real geometry work and is
not done yet; a sentence the customer can act on is.

AND NOTHING IS SAID TO A PART THAT IS NOT PLATE-SHAPED. The first version of this told the Ahmed
reference body "no coolant was found... send the part with its lid on", which is nonsense for a
car. Having no cavity is the normal state of a solid body.
"""
from __future__ import annotations

import pytest

from meshpipeline.cad.fluid_volume_check import (
    BOTH_ENDS,
    LEAKING,
    MANY_HOLES,
    NO_HOLE,
    PLATE_ASPECT,
    SEALED,
    fold,
    plate_like,
    plate_with_too_few_mouths,
    why_no_coolant,
)

#: a cold plate, and a car, by the numbers the measuring step writes (millimetres)
PLATE = {"solids": 1, "size_mm": [19.9, 32.5, 1.1], "flow": "external", "openings": []}
CAR = {"solids": 1, "size_mm": [1044.0, 389.0, 288.0], "flow": "external", "openings": []}


# #
# which parts get told anything at all
# #
def test_a_plate_is_plate_shaped_and_a_car_is_not():
    assert plate_like(PLATE)
    assert not plate_like(CAR)
    # measured thinnest-over-longest: the corpus plates 0.006 to 0.034, the Ahmed body 0.276
    assert 1.1 / 32.5 <= PLATE_ASPECT < 288.0 / 1044.0


def test_a_car_is_told_nothing_about_coolant():
    """Having no cavity is the normal state of a solid body. The first version of this told the
    Ahmed body to send the part with its lid on."""
    for answer in ({"found": False, "caps": 0},
                   {"found": False, "caps": 526},
                   {"found": False, "caps": 4, "pockets_left_out": 1},
                   {"found": False, "caps": 4, "leaks_mm": [[1.0, 2.0, 3.0]]}):
        assert why_no_coolant(answer, CAR) == ""


def test_an_assembly_is_told_nothing_either():
    # plate_like wants ONE solid; a file of several parts has its own, older note.
    assert not plate_like({**PLATE, "solids": 4})
    assert why_no_coolant({"found": False, "caps": 0}, {**PLATE, "solids": 4}) == ""


def test_a_crash_is_not_blamed_on_the_geometry():
    assert why_no_coolant({"error": "TimeoutError: ..."}, PLATE) == ""


# #
# the four reasons, each read off what was measured
# #
def test_no_hole_in_any_face_is_an_open_top_groove():
    # lee_garimella: the split ran and found no cap candidate at all.
    assert why_no_coolant({"found": False, "caps": 0}, PLATE) == NO_HOLE
    assert "lid" in NO_HOLE and "grooves" in NO_HOLE


def test_a_sealed_cavity_is_told_apart_from_having_no_holes():
    """ORDER MATTERS AND THIS IS WHY. The branching plate measures caps 0 AND pockets 1, and with
    the checks the other way round it was told its grooves were open at the top when what it
    actually has is a closed network inside it."""
    branching = {"found": False, "caps": 0, "pockets_left_out": 1}
    assert why_no_coolant(branching, PLATE) == SEALED
    assert "sealed" in SEALED


def test_a_leak_is_told_apart_too():
    assert why_no_coolant(
        {"found": False, "caps": 4, "leaks_mm": [[1.0, 2.0, 3.0]]}, PLATE) == LEAKING


def test_holes_found_but_the_space_still_open_names_the_count():
    # pin_fin: every pin's end is a candidate, so 526 of them, and the chamber is open across a
    # whole face rather than running as channels.
    said = why_no_coolant({"found": False, "caps": 526}, PLATE)
    assert said == MANY_HOLES.format(n=526)
    assert "526" in said and "pin-fin" in said


def test_the_four_reasons_are_four_different_sentences():
    assert len({NO_HOLE, SEALED, LEAKING, MANY_HOLES}) == 4


# #
# ...and the case where the split was never run
# #
def test_a_plate_with_too_few_mouths_is_told_why_nothing_was_looked_for():
    # corintis found 0 mouths and tuckerman_pease 1, so neither reached the split.
    assert BOTH_ENDS == 2
    none_found = plate_with_too_few_mouths(PLATE)
    one_found = plate_with_too_few_mouths({**PLATE, "openings": [{"id": 0}]})
    assert "no hole was found" in none_found
    assert "only one hole was found" in one_found
    assert "was found" in one_found, "it read 'only 0 holes was found' before"


def test_a_plate_with_both_ends_is_not_told_anything():
    assert plate_with_too_few_mouths({**PLATE, "openings": [{"id": 0}, {"id": 1}]}) == ""


def test_a_car_with_one_mouth_is_not_told_anything():
    assert plate_with_too_few_mouths({**CAR, "openings": [{"id": 0}]}) == ""


def test_a_sticker_the_user_marked_not_an_opening_does_not_count():
    facts = {**PLATE, "openings": [{"id": 0}, {"id": 1, "role": "not_an_opening"}]}
    assert plate_with_too_few_mouths(facts) != ""


# #
# and the reason reaches the notes the stage shows
# #
def test_the_reason_is_put_in_the_notes():
    facts = {**PLATE, "notes": []}
    fold(facts, {"found": False, "caps": 0, "reason": "something terse and internal"})
    assert facts["notes"] == [NO_HOLE]


def test_the_older_note_is_not_said_as_well():
    # One reason, not two. The generic "could not be found, add or move them on the picture" note
    # is for the paths this does not cover.
    facts = {**PLATE, "flow": "internal", "notes": []}
    fold(facts, {"found": False, "caps": 526})
    assert len(facts["notes"]) == 1
    assert facts["notes"][0] == MANY_HOLES.format(n=526)


def test_a_part_this_says_nothing_about_keeps_the_note_it_had():
    facts = {**CAR, "flow": "internal", "notes": []}
    fold(facts, {"found": False, "caps": 0, "reason": "no space inside the parts"})
    assert len(facts["notes"]) == 1
    assert "could not be found" in facts["notes"][0]
    assert facts["notes"][0] not in (NO_HOLE, SEALED, LEAKING)
