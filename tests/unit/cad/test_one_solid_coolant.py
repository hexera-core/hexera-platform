"""One solid with channels through it: the coolant is found, and the part reads as a cooled solid.

A cold plate is one block with channels bored through it. The check only ever split a file of
SEVERAL parts, so a cold plate kept the scout's face-by-face reading and its coolant was never
found - which also meant the one input kind that means conjugate heat transfer was unreachable
from an uploaded plate.

MEASURED through this path (scout, then read_fluid_volume with the real child process and
budget) on the 11-part corpus, 2026-10-25. The coolant volume against what the CAD was built to:

    part                              known mm3    found mm3   error      s
    unit_cell_100um                      2.600000     2.6000   exact     0.7
    hotspot_array                       10.080000    10.0800   exact     1.3
    manifold_two_level                   4.560000     4.5600   exact     1.2
    wavy_250um_16ch                     43.675529    43.6752   -0.001%  34.5
    murray_ported_4gen                   6.390617     6.3697   -0.33%    1.6
    embedded_silicon_D9_10um             0.750000     0.7434   -0.88%    8.4

Six of eleven, four of them exact. What the split contributes for one solid is the VOLUME, the
caps and a point inside the coolant; it does not touch the openings or the kind, because for one
solid the scout measured that part's own faces and a pipe tee showed what overriding that costs.
The other five are refused
for understood reasons, not silently: corintis (0 mouths found) and tuckerman_pease (1) never
reach the split, and lee_garimella reaches it and finds nothing - all three are OPEN-TOP GROOVES
lidded at mesh time, so their channel mouths are notches in the part's outline rather than holes
in a face, and the extractor only caps a hole. pin_fin leaks and murray_branching's cavity is
sealed, so it comes back as a pocket. Each of those keeps the reading it already had.

The embedded-silicon error is its two edge channels, which are flush with the end faces and so
are notches too: 198 of 200. murray_ported's is tessellation, not extraction.
"""
from __future__ import annotations

import pytest

from meshpipeline.cad.fluid_volume_check import (
    MIN_PARTS,
    MIN_PASSAGE_MOUTHS,
    _litres,
    fold,
    has_passage_mouths,
    wants_fluid_volume,
)

MM3_PER_L = 1e6


def _facts(solids: int, mouths: int, *, flow: str = "internal", body: str = "pipe_wall") -> dict:
    return {"solids": solids, "body_kind": body, "input_kind": "solid-body", "flow": flow,
            "openings": [{"id": i, "role": "inlet"} for i in range(mouths)],
            "notes": [], "confidence": {}}


# #
# which files the split is spent on
# #
def test_one_solid_with_two_mouths_is_split():
    assert MIN_PASSAGE_MOUTHS == 2
    assert wants_fluid_volume(_facts(1, 2))
    assert wants_fluid_volume(_facts(1, 17))


def test_one_mouth_is_a_bore_and_is_not_split():
    # A passage needs a way in and a way out; one hole is a bore or a bolt hole.
    assert not wants_fluid_volume(_facts(1, 1))
    assert not wants_fluid_volume(_facts(1, 0))


def test_the_flow_the_measuring_step_guessed_is_not_consulted():
    # A branching plate fed through one side read as EXTERNAL with three mouths and extracts
    # cleanly (6.3697 mm3); an embedded-silicon array with 200 channels read as external with
    # two. Requiring "internal" here threw both of them away.
    assert has_passage_mouths(_facts(1, 3, flow="external", body="single_solid"))
    assert wants_fluid_volume(_facts(1, 2, flow="external", body="single_solid"))


def test_what_was_split_before_is_still_split():
    assert MIN_PARTS == 2
    assert wants_fluid_volume(_facts(2, 0))          # an assembly, as before
    assert wants_fluid_volume(_facts(9, 0))
    assert wants_fluid_volume({"solids": 0, "body_kind": "surface"})   # a file of surfaces
    assert not wants_fluid_volume({"solids": 0, "body_kind": "single_solid"})


# #
# what one solid's cavity makes the part
# #
def _answer(volume_mm3: float, mouths: int = 2) -> dict:
    port = {"centroid_mm": [0.0, 0.0, 0.0], "normal": [0.0, 1.0, 0.0], "area_mm2": 0.05,
            "hole_mm2": 0.05, "wh_mm": [0.1, 0.5], "on_extremity": True, "clear_ahead": True,
            "gap": None, "proposed": True}
    return {"found": True, "parts": 1, "caps": mouths, "regions": 1, "seconds": 0.7,
            "passages": [{"volume_l": volume_mm3 / MM3_PER_L, "volume_mm3": volume_mm3,
                          "seed_mm": [1.0, 5.0, 0.5],
                          "openings": [dict(port, centroid_mm=[0.0, y, 0.0])
                                       for y in (0.0, 10.0)[:mouths]]}]}


def test_the_split_hands_over_the_volume_and_the_seed():
    # What one solid's split contributes: the volume and a point inside the coolant. Those are
    # the two things a conjugate case needs that the part's faces cannot give.
    facts = _facts(1, 2)
    fold(facts, _answer(2.6))
    passage = facts["fluid_volume"]["passages"][0]
    assert passage["volume_mm3"] == pytest.approx(2.6)
    assert passage["seed_mm"] == [1.0, 5.0, 0.5]
    assert facts["fluid_volume"]["caps"] == 2
    # ...and the scout's own seed is left where it was, like the openings
    assert "seed_point_mm" not in facts
    assert any("2.6 mm3" in n for n in facts["notes"]), facts["notes"]


def test_one_solid_keeps_the_reading_it_had():
    """THE SPLIT OVERRULES NOTHING FOR ONE SOLID, and a pipe tee is why. It replaces the openings
    of an ASSEMBLY because the scout has no good reading to start from there - dozens of flat
    faces over several parts, and no way to tell which bound the fluid. For one solid it measured
    that solid's own faces directly. A tee had a correct pipe_wall reading with its three ports,
    and overriding it replaced that with "the space inside the parts"."""
    facts = _facts(1, 3)
    before = {k: facts[k] for k in ("openings", "input_kind", "body_kind", "flow")}
    fold(facts, _answer(2.6, mouths=2))
    assert facts["openings"] == before["openings"], "the tee's own three ports"
    assert facts["input_kind"] == before["input_kind"]
    assert facts["body_kind"] == before["body_kind"] == "pipe_wall"
    assert facts["flow"] == before["flow"]
    assert "openings_from" not in facts
    # ...and the stage can still offer the cooled-solid reading, because mouths were found
    from meshpipeline.application.geometry_check import _kinds_on_offer
    assert "solid-assembly" in _kinds_on_offer(facts, {"openings": facts["openings"]})


def test_an_assembly_is_still_read_as_the_cavity_it_was():
    # Several parts with the fluid among them is the reading that existed, and it has not moved.
    facts = _facts(4, 2)
    fold(facts, _answer(1.5e6))
    assert facts["openings_from"] == "fluid_volume"
    assert facts["seed_point_mm"] == [1.0, 5.0, 0.5]
    assert facts["input_kind"] == "body-surface"
    assert facts["body_kind"] == "assembly_cavity"


def test_a_split_that_finds_nothing_leaves_the_reading_alone():
    facts = _facts(1, 2)
    before = dict(facts)
    fold(facts, {"found": False, "reason": "no space inside the parts"})
    assert facts["input_kind"] == before["input_kind"]
    assert facts["body_kind"] == before["body_kind"]


# #
# and the volume survives being reported
# #
def test_a_chip_scale_volume_is_not_rounded_to_nothing():
    # 2.6 mm3 is 0.0000026 L. Rounded to four places that is 0.0, and the check used to tell the
    # user their part held no fluid while holding a correct extraction of it.
    facts = _facts(1, 2)
    fold(facts, _answer(2.6))
    passage = facts["fluid_volume"]["passages"][0]
    assert passage["volume_mm3"] == pytest.approx(2.6)
    assert passage["volume_l"] > 0.0
    assert round(2.6 / MM3_PER_L, 4) == 0.0, "the rounding this guards against"


@pytest.mark.parametrize(("litres", "says"), [
    (2.5, "2.5 litres"),
    (0.25, "250 ml"),
    (0.001, "1 ml"),
    (2.6 / MM3_PER_L, "2.6 mm3"),        # a cold plate's coolant
    (43.6755 / MM3_PER_L, "43.7 mm3"),   # the wavy plate's
])
def test_the_volume_is_said_in_a_unit_a_person_would_use(litres, says):
    assert _litres(litres) == says


def test_an_assembly_still_says_its_volume_in_the_note():
    facts = _facts(4, 2)
    fold(facts, _answer(1.5e6))              # 1.5e6 mm3 is 1.5 litres
    assert any("1.5 litres" in n for n in facts["notes"]), facts["notes"]
