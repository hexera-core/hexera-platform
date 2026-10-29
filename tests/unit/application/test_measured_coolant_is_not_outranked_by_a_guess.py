"""A split that measured the coolant beats a reading made off flat faces before it ran.

MEASURED END TO END on the first customer assembly, 2026-10-29 - ColdPlateAssembly_CLEAN.step,
six solids, 177.5 x 41.84 x 101.0 mm, through the page as a customer:

    split      found 52,144.56 mm3 of coolant, 3 openings, 379.5 s
    scout      body_kind single_solid, solid-body, EXTERNAL, confidence 0.70 (box_like)
    naming     "cold plate with internal coolant passages", solid-assembly, internal, 0.62
    proposal   solid-body, EXTERNAL

The naming was never consulted: `_proposal` takes the model's answer only when its confidence
clears the scout's, and 0.62 is under 0.70. So both the kind AND the flow stayed the flat-face
guess, on a part whose coolant had just been cut out and measured. External flow then means
`geometry_confirmation.patches_from` returns no openings at all, and the intake correctly refuses
a conjugate case on a geometry with no coolant in it.

`_kinds_on_offer` already holds the principle, in its own words: "COOLANT ACTUALLY FOUND INSIDE
THE PART OUTRANKS THE FLOW THE MEASURING STEP GUESSED ... A split that closed the space off and
measured a volume is far better evidence than a guess made off flat faces." It decides what MAY be
named; the confidence comparison decides whether the naming is heard at all, and that is where the
principle was missing.
"""
from __future__ import annotations

import pytest

from meshpipeline.application.geometry_check import _kinds_on_offer, _proposal

#: the scout's own reading of that plate: a box in a flow, and sure enough of it to win on numbers
SCOUT = {
    "flow": "external",
    "input_kind": "solid-body",
    "body_kind": "single_solid",
    "solids": 6,
    "size_mm": [177.5, 41.84, 101.0],
    "scale_to_m": 0.001,
    "confidence": {"input_kind": 0.70},
    "openings": [{"id": 1, "role": "inlet"}, {"id": 2, "role": "outlet"}, {"id": 3, "role": "outlet"}],
    "notes": [],
}
#: what the pictures said, and what it was worth
VISION = {"input_kind": "solid-assembly", "flow": "internal", "confidence": 0.62,
          "part": "cold plate with internal coolant passages"}

FOUND = {"found": True, "parts": 6, "passages": [{"volume_mm3": 52144.56}]}


def _facts(**over):
    f = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
         for k, v in SCOUT.items()}
    f.update(over)
    return f


def test_the_customers_plate_comes_out_a_cooled_assembly():
    """The whole point: 52 ml measured inside it, named a cold plate, and it must not be offered
    as a solid body in a stream."""
    p = _proposal(_facts(fluid_volume=FOUND), dict(VISION))
    assert p["input_kind"] == "solid-assembly"
    assert p["flow"] == "internal"


def test_without_the_coolant_the_scouts_reading_stands():
    """The guard is the MEASUREMENT, not the model's opinion. No split result, and a model under
    the scout's confidence loses exactly as it did before - which is what keeps a car body from
    being read as a cold plate."""
    p = _proposal(_facts(), dict(VISION))
    assert p["input_kind"] == "solid-body"
    assert p["flow"] == "external"


def test_a_split_that_found_nothing_is_not_evidence():
    p = _proposal(_facts(fluid_volume={"found": False}), dict(VISION))
    assert p["input_kind"] == "solid-body"


def test_one_mouth_is_a_bore_and_not_a_passage():
    """solid-assembly is on offer only past MIN_ASSEMBLY_OPENINGS: one opening is a bolt hole, and
    coolant needs a way in and a way out. The measurement cannot promote what is not on offer."""
    facts = _facts(fluid_volume=FOUND, openings=[{"id": 1, "role": "inlet"}])
    assert "solid-assembly" not in _kinds_on_offer(facts, {"openings": facts["openings"]})
    p = _proposal(facts, dict(VISION))
    assert p["input_kind"] == "solid-body"


def test_only_the_cooled_kind_rides_on_the_measurement():
    """A model under the scout's confidence naming something else still loses. The measurement
    says there is coolant in there; it says nothing about a part being a pipe wall."""
    p = _proposal(_facts(fluid_volume=FOUND),
                  {"input_kind": "body-surface", "flow": "internal", "confidence": 0.62})
    assert p["input_kind"] == "solid-body", "a measurement about coolant promoted an unrelated kind"


def test_a_confident_model_still_wins_the_ordinary_way():
    """Nothing about the ordinary path changes: over the scout's number, the naming is heard."""
    p = _proposal(_facts(fluid_volume=FOUND),
                  {"input_kind": "solid-assembly", "flow": "internal", "confidence": 0.95})
    assert p["input_kind"] == "solid-assembly"
    assert p["flow"] == "internal"


def test_a_kind_already_settled_is_left_exactly_as_it_is():
    """`declared_kind` is the facts ALREADY reading solid-assembly - the question is closed - and
    it short-circuits the whole block. A measurement does not re-open it, and neither does a
    confident model wanting to go the other way."""
    p = _proposal(_facts(fluid_volume=FOUND, input_kind="solid-assembly"),
                  {"input_kind": "solid-body", "flow": "external", "confidence": 0.99})
    assert p["input_kind"] == "solid-assembly", "a settled kind was overruled"
