# Responsibility: Verify the intake prompt has a cooled-solid block, that it is keyed on the words
# a real cooled-solid confirmation actually produces, and that the role vocabulary it states is the
# one the submission validator accepts for this purpose.
# Boundaries: it composes the real prompt and runs the real confirmation and the real validator.
# Nothing here reads a source file for a string.
"""The two declared cases that are not ordinary CFD each had a prompt block; the third had none.

An ECXML thermal model gets `_block_thermal_model` ("A THERMAL MODEL - EVERYTHING IS SETTLED, ASK
NOTHING") and an FEA part gets `_block_fea_path` ("THE FEA PATH - ONE QUESTION, NOTHING
INVENTED"). Each names the opening phrase of its own declaration, says what that declaration
settled, and lists what must never be asked again. A COOLED SOLID - one part whose own passages
carry the coolant, which is what a semiconductor cold plate is - had nothing of the kind, and
`geometry_confirmation.declared_case` reads it as the SAME case as a thermal model (conjugate heat
transfer, an assembly of parts). That is the one block whose instructions are wrong for it: it
says to send NO patches and to ask nothing, while a cold plate's inlet and outlet are the two
things that must be asked.

MEASURED ON THE COMPOSED PROMPT, 2026-11-12, before this block: 0 occurrences of microchannel,
junction temperature, thermal interface, heat flux, W/cm, semiconductor or pitch. Every
cold-plate sentence in it arrived from the engine's own descriptor, intake_guidance and
intake_advisories, injected into the ENGINE FIRST block - which is about the MESHER, and read only
while the engine is being settled. Nothing told the model what the CASE was.

AND THE ROLE VOCABULARY TOLD IT THE WRONG THING. The conjugate purpose declares
(inlet, outlet, wall, external, symmetry, empty) and has NO farfield. The static prompt
(prompts/intake/system.txt, which this branch does not own) says the type is one of
{wall, inlet, outlet, farfield, symmetry, empty} and asks for "one wall PLUS either both
inlet+outlet or a single combined farfield". So a farfield on a cold plate was invited by the
static prompt and refused by the validator, and nothing anywhere said what `external` means on
one. Both halves are asserted below by RUNNING the validator rather than by reading either text.
"""
from __future__ import annotations

import pytest

import meshpipeline.agents.intake.agent as intake
from meshpipeline.application.geometry_confirmation import (
    COOLED_SOLID_WORDS,
    body_of,
    confirmation_message,
    cooled_solid_declared,
    declared_case,
    patches_from,
    thermal_confirmation_message,
    thermal_declared,
    with_declaration,
)
from meshpipeline.engines.purposes import PURPOSES

CHT = "conjugate_heat_transfer"

#: The Corintis plate as the check confirmed it: two solids brazed into one piece, the coolant cut
#: out of them, two 6 mm ports on the same face (project_corintis_port_roles: port_a is the outlet).
PLATE = {
    "flow": "internal", "input_kind": "solid-assembly", "solids": 2, "coolant_inside": True,
    "part": "ColdPlateAssembly_CLEAN.step", "size_mm": [177.5, 41.84, 101.0],
    "unit": "mm", "scale_to_m": 0.001,
    "openings": [
        {"id": 1, "name": "port_a", "role": "outlet", "diameter_mm": 6.0,
         "centroid_mm": [177.0, 20.0, 50.0]},
        {"id": 2, "name": "port_b", "role": "inlet", "diameter_mm": 6.0,
         "centroid_mm": [177.0, 20.0, 80.0]},
    ],
}
#: ...and a thermal model's air box, which is the SAME declared case and a different block.
AIR_BOX = {"check_mode": "thermal", "input_kind": "solid-assembly", "flow": "internal",
           "part": "set_top_box", "regions": ["board", "psu", "air"], "solids": 3,
           "coolant_inside": False, "detail": "standard", "size_mm": [300.0, 60.0, 200.0],
           "unit": "mm", "scale_to_m": 0.001, "unit_fixed": True,
           "unit_fixed_reason": "the model's own lengths are metres"}

SYSTEM = intake.compose_intake_system()
BLOCK = intake._block_cooled_solid()


def _plate_chat():
    return with_declaration([{"role": "user", "content": "mesh this cold plate"}],
                            confirmation_message(body_of(dict(PLATE))))


# #
# the block exists, is registered, and is composed
# #
def test_the_block_is_registered_beside_the_other_two_declared_cases():
    names = [b[0] for b in intake.INTAKE_PROMPT_BLOCKS]
    assert "cooled_solid" in names, "a cooled solid still has no block of its own"
    # beside the cases it is the third of, and before the general asking rules it overrides
    assert names.index("cooled_solid") == names.index("fea_path") + 1
    assert names.index("cooled_solid") < names.index("propose_first")
    purpose = next(p for n, p, _b in intake.INTAKE_PROMPT_BLOCKS if n == "cooled_solid")
    assert purpose.strip(), "the registry entry says nothing about what the block is for"


def test_the_block_reaches_the_model():
    assert BLOCK in SYSTEM


# #
# ...and it is keyed on the words a real confirmation actually says
# #
def test_the_phrase_the_block_keys_on_is_the_one_a_real_cold_plate_declaration_carries():
    """The block tells the model to look for a phrase. If the confirmation stopped saying it, the
    block would be dead and a grep of the prompt would not notice. So the phrase is taken from the
    confirmation the application produces for a real plate."""
    said = confirmation_message(body_of(dict(PLATE)))
    assert COOLED_SOLID_WORDS in said, said
    assert f"'{COOLED_SOLID_WORDS}'" in BLOCK, "the block quotes a phrase the declaration lacks"
    assert cooled_solid_declared(_plate_chat())


def test_the_case_is_the_one_the_block_states():
    assert declared_case(_plate_chat()) == (CHT, "solid-assembly")
    assert "conjugate heat transfer" in BLOCK


def test_a_thermal_model_is_not_read_as_a_cooled_solid_and_the_two_blocks_do_not_overlap():
    """Both are conjugate heat transfer and both are an assembly of parts, so `declared_case`
    cannot separate them - which is the reason the cooled solid needed its own block rather than a
    clause in the thermal one. The declarations DO separate them, and that is what is asserted."""
    box = with_declaration([], thermal_confirmation_message(body_of(dict(AIR_BOX))))
    assert declared_case(box) == declared_case(_plate_chat()) == (CHT, "solid-assembly")
    assert thermal_declared(box) and not cooled_solid_declared(box)
    assert cooled_solid_declared(_plate_chat()) and not thermal_declared(_plate_chat())


def test_an_ordinary_internal_cfd_part_is_not_read_as_a_cooled_solid():
    plain = dict(PLATE, input_kind="body-surface", coolant_inside=None, solids=1)
    chat = with_declaration([], confirmation_message(body_of(plain)))
    assert not cooled_solid_declared(chat)
    assert declared_case(chat) == ("internal_cfd", "body-surface")


# #
# the role vocabulary: what the block states is what the validator takes
# #
def _submit(patches):
    """A complete conjugate submission with these patches, through the real validator."""
    from meshpipeline.agents.intake.validation import validate_submission
    request = ("A conjugate mesh of the coolant and the metal of this cold plate, with the two "
               "ports under the names the user gave them and water at 2 L/min. " * 2)
    brief = ("Both regions present and meshed whole, the surface between them conformal, every "
             "passage resolved across its narrowest width, each region checked clean. " * 2)
    return validate_submission({
        "domain": "cold plate conjugate", "request_txt": request, "review_brief_txt": brief,
        "dimensionality": "3D", "mesh_fidelity": "standard", "engine_source": "user_direct",
        "mesh_engine": "cfmesh_conjugate", "purpose": CHT, "input_kind": "solid-assembly",
        "engine_params": {}, "patches": patches})


def test_the_roles_the_block_names_are_exactly_the_purposes_own():
    declared = set(PURPOSES[CHT].boundary_roles)
    assert declared == {"inlet", "outlet", "wall", "external", "symmetry", "empty"}
    assert "farfield" not in declared
    for role in declared:
        assert role in BLOCK, f"the block never mentions the role {role!r} this purpose declares"


def test_the_patches_the_check_declares_are_accepted_as_they_stand():
    """The block says to carry exactly what the message declares. Run it: the application's own
    patch list for this plate goes through the validator untouched."""
    declared = patches_from(body_of(dict(PLATE)))
    assert [p["type"] for p in declared] == ["outlet", "inlet", "wall"], declared
    assert _submit([{"name": p["name"], "type": p["type"]} for p in declared]) == []


def _role_enums() -> list[list[str]]:
    """Every `type` enum the submit tools declare for a patch, found by walking the real schema
    rather than by indexing into it - the tool order is not this file's business."""
    found: list[list[str]] = []

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("enum"), list) and "wall" in node["enum"]:
                found.append(list(node["enum"]))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(intake.INTAKE_TOOLS)
    return found


def test_the_tool_schema_describes_this_cases_roles_and_the_validator_agrees():
    """THE ROLE DESCRIPTION IN THE TOOL SCHEMA IS AN INSTRUCTION TOO, and it is the one the model
    reads while filling the submission in. It said nothing about a cooled solid, so the only
    guidance for the `type` field was the general CFD sentence - which asks for "a single combined
    farfield", the one role this purpose refuses.

    Held to the purpose's OWN roles, read out of PURPOSES, not to a copy of the text: every role
    this purpose declares has to be named in the description, and `farfield` has to be in the
    schema's enum (external CFD needs it) while not being one of this purpose's."""
    enums = _role_enums()
    assert enums, "no patch role enum was found in the tool schema at all"
    declared = set(PURPOSES[CHT].boundary_roles)
    text = next(d for d in _descriptions() if "OF A COOLED SOLID" in d)
    for role in declared:
        assert f"'{role}'" in text, f"the schema never names the role {role!r} on this case"
    assert "NO 'farfield'" in text, text
    for enum in enums:
        assert declared <= set(enum), (declared - set(enum), enum)
        assert "farfield" in enum, "external CFD lost its own role from the enum"
    assert "farfield" not in declared
    # ...and the validator is the authority the description is describing
    assert _submit([{"name": "ff", "type": "farfield"}]), "farfield passed the validator"


def _descriptions() -> list[str]:
    out: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            d = node.get("description")
            if isinstance(d, str):
                out.append(d)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(intake.INTAKE_TOOLS)
    return out


def test_a_farfield_on_a_cold_plate_is_really_refused():
    """Which is why the block has to say so: the static prompt asks for "a single combined
    farfield", and this purpose has no such role."""
    errs = _submit([{"name": "plate", "type": "wall"}, {"name": "ff", "type": "farfield"}])
    assert errs, "a farfield was accepted, so the block is warning about nothing"
    assert any("farfield" in e for e in errs), errs
    assert "NO farfield" in BLOCK


def test_external_is_accepted_and_the_block_says_what_it_is():
    """It is in the purpose's roles and in the submit schema's enum, and nothing told the model
    what it means on a cooled solid. The engine writes it as an OpenFOAM wall."""
    assert _submit([{"name": "port_b", "type": "inlet"}, {"name": "port_a", "type": "outlet"},
                    {"name": "plate_wall", "type": "wall"},
                    {"name": "lid_top", "type": "external"}]) == []
    low = BLOCK.lower()
    assert "faces the room" in low and "rather than the coolant" in low
    from meshpipeline.engines.cfmesh_conjugate.conjugate import ROLE_TO_OF_TYPE
    assert ROLE_TO_OF_TYPE["external"] == "wall"


# #
# and what it forbids being asked
# #
@pytest.mark.parametrize("never", ["far field", "domain size", "reference length",
                                   "air box", "which way air moves past it"])
def test_the_block_names_what_a_cooled_solid_has_none_of(never):
    assert never in BLOCK, never


def test_the_block_does_not_repeat_what_the_engine_already_declares():
    """The engine's own intake_guidance and advisories are injected into ENGINE FIRST and already
    say which faces the ports are measured from, the lid requirement and the two region names. A
    block that said them again would be two authorities on one fact."""
    from meshpipeline.engines.registry import get_spec
    spec = get_spec("cfmesh_conjugate")
    for advisory in spec.intake_advisories:
        assert advisory not in BLOCK, advisory
    assert spec.intake_guidance not in BLOCK
    assert spec.intake_guidance in SYSTEM, "the engine's guidance stopped reaching the prompt"


def test_the_coolant_is_asked_about_as_a_liquid_and_not_as_air():
    low = BLOCK.lower()
    assert "l/min" in low
    for air in ("air speed", "freestream velocity", "mach number"):
        assert air in low, air
    # ...and the words a cold plate's owner must never be shown, forbidden in the reply and in
    # both of the texts that outlive the chat
    assert "never write far field" in low
    for where in ("a reply", "request_txt", "review_brief_txt"):
        assert where in BLOCK, where


def test_the_purpose_map_block_describes_a_cold_plate_too():
    """It routed one here and then described the other case: "an assembly of solid parts" is a
    thermal model's air box, and a cold plate's owner reads that as a denial."""
    block = intake._block_purpose_map()
    assert "cold plate" in block
    assert "one part with coolant passages through it" in block
