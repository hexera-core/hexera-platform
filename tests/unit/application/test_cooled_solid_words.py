"""A cold plate's owner is told about a cold plate, and is not sent away for a file he lacks.

Three things the platform said to someone uploading a channelled cold plate, each of which would
have stopped him before any mesh was built, and none of them a meshing problem:

  * The stage's first option read "the part's wall, hollow inside for the fluid", which describes
    a cold plate exactly - and means the metal is thrown away and only the water meshed. The one
    that fits was last and read "an assembly of solid parts, each its own region", which someone
    holding one block reads as a denial.
  * The chat answered "mesh this cold plate" with "upload the ECXML model exported from your
    thermal tool. For this file I'll set up the cooling air." He has no thermal tool, and his
    coolant is water inside metal rather than air around a board.
  * The confirmation told him "the fluid around them is one more region" - the opposite of a part
    whose coolant is inside it - and the input kind's own definition said "a multi-solid CAD
    assembly (one closed solid per region)", which one solid contradicts.

The kind covers two shapes of file, so the words branch wherever the code knows which it has. The
stage's dropdown cannot branch, because the user picks from it before anything is settled, so there
one label covers both and says what gets meshed.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from meshpipeline.application import purpose_map as pm
from meshpipeline.application.geometry_confirmation import (
    assembly_sentence,
    kind_words,
)


def _body(**kw) -> SimpleNamespace:
    return SimpleNamespace(**{"input_kind": "solid-assembly", "regions": [], "solids": 1, **kw})


# #
# what the file is called
# #
def test_one_channelled_solid_is_named_as_one():
    assert kind_words(_body()) == "a solid part with coolant passages through it"


@pytest.mark.parametrize("body", [
    _body(regions=["board", "chip", "air"], solids=7),     # a thermal model names its regions
    _body(solids=4),                                       # a plain multi-part assembly
])
def test_a_file_of_several_parts_is_still_named_as_one(body):
    # The ECXML path's wording is unchanged; its own test asserts the whole sentence.
    assert kind_words(body) == "an assembly of solid parts, each its own region"


def test_every_other_kind_is_untouched():
    for kind, said in (("body-surface", "the part's wall, hollow inside for the fluid"),
                       ("fluid-domain", "the fluid volume itself"),
                       ("solid-body", "a solid body")):
        assert kind_words(SimpleNamespace(input_kind=kind, regions=[], solids=1)) == said


# #
# what the confirmation tells him
# #
def test_the_coolant_is_inside_the_part_not_around_it():
    said = assembly_sentence(_body())
    assert "inside it" in said
    assert "around them" not in said, "that is a thermal model's air box, not a cold plate"
    assert "conjugate heat-transfer" in said


def test_a_thermal_model_is_still_told_the_fluid_is_around_its_parts():
    said = assembly_sentence(_body(regions=["board", "air"], solids=7))
    assert "the fluid around them" in said
    assert "2 regions" in said and "board" in said


def test_both_sentences_say_both_halves_get_meshed():
    for body in (_body(), _body(regions=["a", "b"], solids=3)):
        assert "meshed" in assembly_sentence(body)


# #
# and the chat does not send him away
# #
def test_the_cooling_note_does_not_ask_for_a_file_he_has_not_got():
    note = pm.ELECTRONICS_NOTE
    assert "coolant passages" in note, "it should say what happens with the file he HAS"
    assert "cooling air" not in note, "a cold plate's coolant is water inside metal"
    assert not note.lower().startswith("for the full"), "the ECXML offer must not lead"
    # the ECXML route is still offered, because it is the fuller one where a thermal tool is in play
    assert "ECXML" in note


def test_the_note_is_what_the_hold_line_carries():
    from meshpipeline.application.geometry_hold import hold_reply
    reading = pm.read_purpose("mesh this cold plate")
    assert reading.route == pm.ROUTE_ELECTRONICS
    said = hold_reply(False, reading=reading)
    assert pm.ELECTRONICS_NOTE in said


# #
# and the kind's own definition admits it
# #
def test_the_input_kind_definition_admits_one_solid():
    import inspect

    from meshpipeline.engines import purposes
    src = inspect.getsource(purposes)
    where = src[src.index('"solid-assembly" ='):][:600]
    assert "ONE solid" in where or "one solid" in where, (
        "solid-assembly was defined as a multi-solid assembly, which a cold plate contradicts")
    assert "solid-assembly" in purposes.INPUT_KINDS
