"""A brazed cold plate is base plus lid, and it was told its coolant was the air around it.

THE DEFECT, confirmed in source before the fix: both `kind_words` and `assembly_sentence` branched
on `solids == 1`. A brazed or diffusion-bonded cold plate - base plus lid - is TWO solids, and it
is the commonest way one is made, so it fell through to:

    "Each solid part is its own solid region and the fluid around them is one more region"

which is the air-box wording for a thermal model and describes the opposite of the part. Three
solids (base, lid, manifold) is the same case again. This is the text the owner confirms, and the
code's own comment already said that telling a cold plate's owner about "the fluid around them"
describes the opposite of their part - it just counted the wrong thing.

WHAT IT COUNTS NOW is whether the extraction found coolant INSIDE the solids
(cad/fluid_volume_check.fold -> facts["fluid_volume"]["found"]), which is a measurement of the
thing the sentence is about. The solid count stands in only for a record written before the field
existed.
"""
from __future__ import annotations

import pytest

from meshpipeline.application.geometry_confirmation import (
    assembly_sentence,
    body_of,
    kind_words,
)

AIR_BOX = "the fluid around them"
INSIDE = "the coolant inside it"


def _body(**kw):
    return body_of({"flow": "internal", "input_kind": "solid-assembly", **kw})


@pytest.mark.parametrize("solids", [1, 2, 3, 9])
def test_coolant_found_inside_is_always_the_cooled_solid_wording(solids):
    said = assembly_sentence(_body(solids=solids, coolant_inside=True))
    assert INSIDE in said, said
    assert AIR_BOX not in said, said
    assert kind_words(_body(solids=solids, coolant_inside=True)) \
        == "a solid part with coolant passages through it"


def test_a_brazed_plate_is_described_as_the_stack_it_is():
    said = assembly_sentence(_body(solids=2, coolant_inside=True))
    assert "2 solids bonded into one piece" in said, said
    assert "a base and a lid" in said, said


def test_parts_standing_in_air_still_get_the_air_box_wording():
    said = assembly_sentence(_body(solids=9, coolant_inside=False))
    assert AIR_BOX in said, said
    assert INSIDE not in said, said
    assert kind_words(_body(solids=9, coolant_inside=False)) \
        == "an assembly of solid parts, each its own region"


def test_a_named_region_file_is_the_files_own_model_either_way():
    """An ECXML thermal model names its regions, and that reading is the file's, not measured."""
    said = assembly_sentence(_body(solids=9, coolant_inside=True, regions=["board", "die"]))
    assert "named as in the file" in said, said


@pytest.mark.parametrize(("solids", "want_inside"), [(1, True), (2, False), (9, False)])
def test_a_record_written_before_the_measurement_falls_back_to_the_count(solids, want_inside):
    said = assembly_sentence(_body(solids=solids))
    assert (INSIDE in said) is want_inside, said


def test_the_confirm_body_carries_the_measurement():
    from meshpipeline.api.v1.geometry import ConfirmIn
    assert "coolant_inside" in ConfirmIn.model_fields, (
        "the field has to exist on the request body or the server cannot fill it, and both "
        "branches then read a default - which is how the dead `solids` field behaved")
    assert ConfirmIn.model_fields["coolant_inside"].default is None, (
        "None means not measured, which is not the same as measured false")
