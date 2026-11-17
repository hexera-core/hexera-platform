# Responsibility: Verify FLOW-1, the user's own words about the flow: a stated flow wins over
# everything measured, the object the words name is the next best word, and words that say both
# (or take a statement back) settle nothing.
# Boundaries: pure text in, a reading out; no CAD, no model.
from __future__ import annotations

import pytest

from meshpipeline.application import flow_kind as fk


@pytest.mark.parametrize("text", [
    "water flows through the manifold", "internal flow, 2 m/s", "the air inside the duct",
    "pressure drop across this valve", "mesh the fluid volume itself - it is the fluid domain",
    "The flow is through the inside.",
])
def test_a_stated_flow_through_the_part_is_internal(text):
    r = fk.words_reading(text)
    assert (r.kind, r.tier) == ("internal", "said")


@pytest.mark.parametrize("text", [
    "air flows around the car at 40 m/s", "external aero of the wing", "drag and lift at 30 m/s",
    "wind tunnel test of the model", "The flow is around the outside.",
])
def test_a_stated_flow_around_the_part_is_external(text):
    r = fk.words_reading(text)
    assert (r.kind, r.tier) == ("external", "said")


def test_the_object_named_is_the_next_best_word():
    assert (fk.words_reading("an exhaust manifold").kind, fk.words_reading("an exhaust manifold").tier) == ("internal", "named")
    assert (fk.words_reading("a race car body").kind, fk.words_reading("a race car body").tier) == ("external", "named")


def test_a_stated_flow_beats_the_object_named():
    """Cross-flow over a pipe bank names a pipe and states a flow around it: the statement wins."""
    assert fk.words_reading("the air flows around the pipe").kind == "external"


@pytest.mark.parametrize("text", ["", "mesh this please", "not an internal flow case",
                                  "flow through the grille and around the car"])
def test_words_that_settle_nothing_say_nothing(text):
    assert fk.words_reading(text).kind == ""
