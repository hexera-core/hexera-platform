"""A part nobody has measured yet is not a solid body because nothing was found inside it.

MEASURED END TO END on ColdPlateAssembly_CLEAN.step, session 7831769b, 2026-11-14 17:56. The
naming step ran out of its wait and named the PREVIEW, whose facts carry `openings: []` and
`confidence {"input_kind": 0.0}` by construction (`preview_facts`) because nothing has been probed
yet. The model read the pictures correctly - `input_kind solid-assembly`, `flow internal`,
confidence 0.72, "the user specifies conjugate heat transfer through a coolant passage and
surrounding metal, so this is a solid assembly rather than an external-flow body" - and the
proposal that reached the user was:

    input_kind  solid-body        <- the preview's placeholder, kept
    flow        internal          <- the model's, taken
    openings    []

`solid-body` is defined in `contracts/geometry_fields` as "a solid body the fluid flows around",
so that card says the fluid flows through a body it flows around. It is not merely odd: on this
deployment `conjugate_heat_transfer + solid-body` admits NO multi-region engine at all - all three
come back "impossible", because `cfmesh_conjugate` declares exactly one capability and it is
`solid-assembly` - so a customer who confirms what they were shown cannot reach a mesh.

`_kinds_on_offer` would not offer `solid-assembly` because the proposal had fewer than
MIN_ASSEMBLY_OPENINGS mouths. That gate is right about a measured part and wrong here: a preview
has no mouths because none have been looked for, which is the same conflation
`cad/fluid_volume_check.fold` had to undo for a split that ran out of time ("one of those two is
the geometry's answer and the other is ours; they are not the same sentence"). What a preview DOES
know is what the file holds - the reading step counts its solids before anything is measured, and
this one holds six.

The companion session 3b3027a5 is why this is a preview defect and not an engine one: there the
full reading landed, `fold` cut 54 ml of coolant out of the six solids, and `merged_onto_full`
rebuilt the proposal as `solid-assembly / internal` with three named mouths. The same file, the
same code, a different clock.

WHAT THIS DOES NOT CHANGE, said so it is not mistaken for covered. A preview of ONE closed solid
whose pictures say `internal` can still be shown with the placeholder kind beside the model's
flow, because one solid is not an assembly of parts on any evidence and the kinds that do mean
internal - `fluid-domain`, `body-surface` - are already on offer and were not what the model
picked. Taking the flow away as well is worse, not better: the flow is the half the model gets
right, `patches_from` returns no openings for external, and the one-solid cold plate reaches
`solid-assembly` through the coolant the split cuts out of it (cad/fluid_volume_check.fold).
"""
from __future__ import annotations

import pytest

from meshpipeline.application.geometry_check import (
    MIN_ASSEMBLY_OPENINGS,
    MIN_ASSEMBLY_SOLIDS,
    _facts_text,
    _kinds_on_offer,
    _proposal,
)


def _preview(solids: int) -> dict:
    """The shape `preview_facts` returns: the part's box and what the file holds, nothing measured."""
    return {"body_kind": "unknown", "input_kind": "solid-body", "flow": "external",
            "solids": solids, "planar_faces": None,
            "bbox_min_m": [0.0, 0.0, 0.0], "bbox_max_m": [0.1775, 0.0418, 0.101],
            "size_mm": [177.5, 41.8, 101.0],
            "openings": [], "seed_point_m": None, "seed_point_mm": None,
            "confidence": {"input_kind": 0.0, "openings": 0.0},
            "notes": ["the part is still being measured: its openings and the clean check follow"],
            "faces": [], "holes": [], "read_as": "cad", "unit_assumed": False,
            "scale_to_m": 0.001, "preview": True}


def _measured(solids: int) -> dict:
    """A part the scout HAS read: it looked at the faces and found no passage mouths."""
    return {**_preview(solids), "body_kind": "single_solid", "preview": False,
            "confidence": {"input_kind": 0.7, "openings": 0.0},
            "notes": []}


#: the model's answer on the measured session, word for word
VISION_PLATE = {"part": "metal component with an internal coolant passage", "flow": "internal",
                "input_kind": "solid-assembly", "openings": [], "confidence": 0.72,
                "real_length_m": 0.18,
                "notes": "The user specifies conjugate heat transfer through a coolant passage "
                         "and surrounding metal, so this is a solid assembly rather than an "
                         "external-flow body."}
#: what the pictures say about a car, which the prompt tells the model to say ("a car body, a wing
#: or a hull is solid-body however many bolt holes it has")
VISION_CAR = {"part": "a car body", "flow": "external", "input_kind": "solid-body",
              "openings": [], "confidence": 0.9, "nose_view": "side", "nose_side": "left"}


# #
# the offer, on a part nothing has been measured about yet
# #
def test_a_preview_of_several_solids_the_fluid_runs_through_may_be_an_assembly():
    assert "solid-assembly" in _kinds_on_offer(_preview(6), {"openings": []}, VISION_PLATE)


def test_a_preview_of_a_car_is_still_a_car():
    # The one guard a preview has left is the model's own reading of the flow: a body the fluid
    # goes AROUND is not an assembly of regions, however many solids the file holds.
    assert "solid-assembly" not in _kinds_on_offer(_preview(62), {"openings": []}, VISION_CAR)


def test_a_preview_of_one_solid_still_waits_for_its_mouths():
    # One closed solid is not an assembly of parts whatever the pictures say; the cold plate bored
    # from a single block reaches `solid-assembly` through the coolant the split cuts out of it.
    assert MIN_ASSEMBLY_SOLIDS == 2
    assert "solid-assembly" not in _kinds_on_offer(_preview(1), {"openings": []}, VISION_PLATE)


def test_a_measured_part_with_too_few_mouths_keeps_what_was_measured():
    # Nothing here is about doubting a reading that exists. The scout looked at this part's faces
    # and found no passage mouths, and that stands.
    assert MIN_ASSEMBLY_OPENINGS == 2
    assert "solid-assembly" not in _kinds_on_offer(_measured(6), {"openings": []}, VISION_PLATE)


def test_the_offer_is_unchanged_when_the_pictures_said_nothing():
    # vision is None before the naming has run, and {"error": ...} when it could not: neither is
    # an answer, so neither widens the offer.
    assert "solid-assembly" not in _kinds_on_offer(_preview(6), {"openings": []}, None)
    assert "solid-assembly" not in _kinds_on_offer(_preview(6), {"openings": []}, {"error": "Timeout"})


@pytest.mark.parametrize("solids,measured", [(6, False), (1, False), (6, True)])
def test_the_three_old_kinds_are_always_on_offer(solids, measured):
    facts = _measured(solids) if measured else _preview(solids)
    for kind in ("body-surface", "fluid-domain", "solid-body"):
        assert kind in _kinds_on_offer(facts, {"openings": []}, VISION_PLATE)


# #
# ...and what the user is actually shown
# #
def test_the_card_the_customer_saw_is_not_served_again():
    got = _proposal(_preview(6), VISION_PLATE)
    assert got["input_kind"] == "solid-assembly"
    assert got["flow"] == "internal"


@pytest.mark.parametrize("solids", [2, 6, 62])
@pytest.mark.parametrize("vision", [VISION_PLATE, VISION_CAR], ids=["plate", "car"])
def test_an_assembly_is_never_shown_as_a_body_the_fluid_flows_through(solids, vision):
    # The contradiction itself, named: `solid-body` IS "a solid body the fluid flows around", so a
    # card carrying it beside "through the part" says both at once.
    got = _proposal(_preview(solids), vision)
    assert not (got["flow"] == "internal" and got["input_kind"] == "solid-body"), \
        (solids, vision["part"], got["input_kind"], got["flow"])


def test_a_car_preview_is_proposed_as_a_car():
    got = _proposal(_preview(62), VISION_CAR)
    assert got["input_kind"] == "solid-body"
    assert got["flow"] == "external"


# #
# and the model is not told the file is one solid when it is six
# #
def test_the_namer_is_not_told_a_six_solid_file_is_one_solid():
    said = _facts_text(_measured(6), [], "")
    assert "single_solid" not in said, said
    assert "6 solids" in said, said


def test_a_one_solid_file_still_reads_as_what_it_is():
    said = _facts_text(_measured(1), [], "")
    assert "single_solid" in said, said
