"""The stage may propose a cooled solid, and a car body can never be one.

A cold plate and a car body are both closed solids in a picture, so the pictures are not allowed
to settle this on their own: the measuring step has to have found passage mouths first. That
matters because the two readings go to different meshers - a cooled solid goes to the one that
waits for a coolant inlet, and a car read as a cooled solid would wait forever.

Before this, "solid-assembly" was reachable only from an ECXML sidecar, which declares its own
regions; a plain STEP cold plate could not be read as one at all.
"""
from __future__ import annotations

import pytest

from meshpipeline.application.geometry_check import (
    MIN_ASSEMBLY_OPENINGS,
    NAME_TOOL,
    _kinds_on_offer,
)
from meshpipeline.engines.purposes import INPUT_KINDS


def _facts(flow: str) -> dict:
    return {"flow": flow, "input_kind": "solid-body", "body_kind": "single_solid"}


def _proposal(openings: int) -> dict:
    return {"openings": [{"id": i, "name": f"o{i}", "role": "inlet"} for i in range(openings)]}


# #
# the offer
# #
def test_a_part_with_passage_mouths_may_be_a_cooled_solid():
    assert "solid-assembly" in _kinds_on_offer(_facts("internal"), _proposal(2))
    assert "solid-assembly" in _kinds_on_offer(_facts("internal"), _proposal(17))
    # the scout does not always call a channelled plate internal - a branching part with its feed
    # in one side read as external flow with three mouths - so the mouths are what decide
    assert "solid-assembly" in _kinds_on_offer(_facts(""), _proposal(3))


def test_a_car_body_can_never_be_a_cooled_solid():
    # No mouths and read as flow around it: whatever the picture model says, the kind is not on
    # offer, so it cannot be set.
    car = _kinds_on_offer(_facts("external"), _proposal(0))
    assert "solid-assembly" not in car
    # ...and not even if the model found holes to call openings. A car read as external stays out.
    assert "solid-assembly" not in _kinds_on_offer(_facts("external"), _proposal(6))


def test_one_mouth_is_a_bore_not_a_passage():
    # Coolant needs a way in and a way out. One hole is a bore or a bolt hole.
    assert MIN_ASSEMBLY_OPENINGS == 2
    assert "solid-assembly" not in _kinds_on_offer(_facts("internal"), _proposal(1))
    assert "solid-assembly" not in _kinds_on_offer(_facts("internal"), _proposal(0))


@pytest.mark.parametrize("flow", ["internal", "external", ""])
def test_the_three_old_kinds_are_always_on_offer(flow):
    # Nothing that could be proposed before has stopped being proposable.
    for kind in ("body-surface", "fluid-domain", "solid-body"):
        assert kind in _kinds_on_offer(_facts(flow), _proposal(0))
        assert kind in _kinds_on_offer(_facts(flow), _proposal(4))


# #
# and the picture model is actually told about it
# #
def test_the_naming_step_can_say_it_and_knows_what_it_means():
    kinds = NAME_TOOL["function"]["parameters"]["properties"]["input_kind"]["enum"]
    assert "solid-assembly" in kinds
    # every kind the tool offers is one the platform has a meaning for
    assert set(kinds) <= set(INPUT_KINDS), set(kinds) - set(INPUT_KINDS)
    said = NAME_TOOL["function"]["parameters"]["properties"]["input_kind"]["description"]
    assert "solid-assembly" in said and "coolant" in said


def test_a_cooled_solid_is_the_one_kind_that_means_conjugate_heat_transfer():
    # The stage's sentence is read back into a purpose, and this is the pairing it makes. Written
    # down here because the two halves live in different modules.
    from meshpipeline.application.geometry_confirmation import CONFIRMED_MARK, declared_case
    said = (f"{CONFIRMED_MARK} input_kind solid-assembly; the fluid flows through it. "
            "Part size: 33 x 20 x 1 mm.")
    assert declared_case([{"role": "assistant", "content": said}]) == (
        "conjugate_heat_transfer", "solid-assembly")
