# Responsibility: Verify the requirements card a COOLED SOLID's owner approves is read off their
# own declaration, and that `cooled_solid_declared` is what reads it.
# Boundaries: it builds the real brief from a real confirmation and runs the real compatibility and
# boundary gates. Nothing here reads a source file for a string.
"""A cold plate and an electronics air box are the same declared case, and the card said so.

`application/geometry_confirmation.declared_case` reads both as (conjugate_heat_transfer,
solid-assembly), so nothing already on the requirements card told a cold-plate owner which of the
two their file was read as. The declaration is the only thing that separates them, and the reader
for it - `cooled_solid_declared` - HAD NO PRODUCTION CALLER AT ALL: its definition, its __all__
entry and all four call sites were inside one test file, so nothing at runtime behaved differently
because a cooled solid had been confirmed.

THE PATTERN IT CLAIMED TO MIRROR DOES NOT LOOK LIKE THAT. `thermal_declared` is read at
application/intake_brief._thermal_view, which is what puts `check_mode` and the job sheet on the
card and what makes two of the card's own checks speak in an electronics-cooling engineer's words
rather than in the registry's keys. `fea_declared` is read at
agents/intake/message.analysis_question_due, which is what makes the application ask the FEA
analysis question. This file is the cooled solid's half of the first of those.

MEASURED 2026-11-12 on a real Corintis plate confirmation, before the wiring - the two rows a
cold-plate owner reads on the card they approve:

    Engine cfmesh_conjugate supports conjugate_heat_transfer from solid-assembly
    3 boundary assignment(s) valid for conjugate_heat_transfer

Both are true and neither is checkable by the person approving them. A thermal model's card has
said its two rows in words since the thermal split; a cooled solid's said them in enum keys. The
VERDICTS are untouched: the same compatibility gate on the same engine, the same role check on the
same roles, asserted below.

WHAT THIS FILE WILL NOT ACCEPT: a test that greps a source file, or one that asserts the reader
returns True with nothing reading it. Every assertion below goes through build_brief or
build_checks.
"""
from __future__ import annotations

from meshpipeline.application.geometry_confirmation import (
    body_of,
    confirmation_message,
    thermal_confirmation_message,
    with_declaration,
)
from meshpipeline.application.intake_brief import FAIL, PASS, build_brief, build_checks

#: The Corintis plate as the check confirmed it: two solids brazed into one piece with the coolant
#: cut out of them, two 6 mm ports on the same face (project_corintis_port_roles: port_a is the
#: outlet). The same dict the intake prompt block's own test uses.
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
#: ...and a thermal model's air box: the SAME declared case, a different declaration.
AIR_BOX = {"check_mode": "thermal", "input_kind": "solid-assembly", "flow": "internal",
           "part": "set_top_box", "regions": ["board", "psu", "air"], "solids": 3,
           "coolant_inside": False, "detail": "standard", "size_mm": [300.0, 60.0, 200.0],
           "unit": "mm", "scale_to_m": 0.001}

SAID = [{"role": "user", "content": "mesh this cold plate"}]


class _Session:
    """A session whose requirements are settled, with whatever chat is handed to it."""

    def __init__(self, messages):
        self.messages = messages
        self.request_txt = ("A conjugate mesh of the coolant and the metal of this cold plate, "
                            "with the two ports under the names the user gave them. " * 2)
        self.review_brief_txt = ("Both regions present and meshed whole, the surface between them "
                                 "conformal, each region checked clean. " * 2)
        self.purpose = "conjugate_heat_transfer"
        self.mesh_engine = "cfmesh_conjugate"
        self.input_kind = "solid-assembly"
        self.dimensionality = "3D"
        self.requested_mesh_fidelity = "standard"
        self.domain = "cold plate conjugate"
        self.intake_patches = [{"name": "port_b", "type": "inlet"},
                               {"name": "port_a", "type": "outlet"},
                               {"name": "wall", "type": "wall"}]
        self.engine_params = {}
        self.intake_submitted = True
        self.intake_gate = {}
        self.llm_metadata = []
        self.owner_id = "user-42"
        self.user_notes = []


def _plate_chat():
    return with_declaration(list(SAID), confirmation_message(body_of(dict(PLATE))))


def _row(brief, cid):
    return next(c for c in brief["checks"] if c["id"] == cid)


def _brief(messages):
    brief = build_brief(_Session(messages))
    assert brief is not None
    return brief


# #
# the reader reaches the card
# #
def test_a_confirmed_cooled_solid_is_marked_on_the_brief():
    """The flag the two rows below read, set from the declaration and from nothing else."""
    assert _brief(_plate_chat()).get("cooled_solid") is True


def test_the_same_requirements_with_no_declaration_are_the_ordinary_card():
    """Absent, not False: the ordinary card is the one with no such row at all, the way
    `check_mode` is absent for everything that is not a thermal model."""
    assert "cooled_solid" not in _brief(list(SAID))


def test_a_thermal_model_is_not_marked_as_a_cooled_solid():
    """Both declarations are (conjugate_heat_transfer, solid-assembly). Only the wording differs,
    and only the wording is read."""
    box = with_declaration([], thermal_confirmation_message(body_of(dict(AIR_BOX))))
    assert "cooled_solid" not in _brief(box)


# #
# ...and changes the two rows its owner reads
# #
def test_the_engine_row_says_what_the_mesher_does_to_the_part():
    plate = _row(_brief(_plate_chat()), "engine_compatible")
    plain = _row(_brief(list(SAID)), "engine_compatible")
    assert plate["label"] != plain["label"], (
        "the engine row is the same on a declared cold plate as on an undeclared one, so the "
        "reader is not reaching it")
    assert "the coolant in its own passages" in plate["label"], plate["label"]
    # the ENUM KEYS are what it used to say, and they are gone from this row
    for key in ("conjugate_heat_transfer", "solid-assembly"):
        assert key not in plate["label"], plate["label"]
    # ...and the verdict is the same gate's: compatibility was never in question
    assert plate["status"] == plain["status"] == PASS


def test_the_boundary_row_names_the_ports_and_the_parts_outside():
    plate = _row(_brief(_plate_chat()), "boundary_roles_valid")
    plain = _row(_brief(list(SAID)), "boundary_roles_valid")
    assert plate["label"] != plain["label"]
    low = plate["label"].lower()
    assert "ports" in low and "outer surface" in low, plate["label"]
    assert plate["status"] == plain["status"] == PASS


def test_a_role_this_purpose_refuses_still_fails_on_a_cooled_solids_card():
    """The wording changed and the gate did not. A farfield has no role on this purpose, and the
    row must still say so rather than read as a cold plate's two ports."""
    session = _Session(_plate_chat())
    session.intake_patches = [{"name": "wall", "type": "wall"},
                              {"name": "ff", "type": "farfield"}]
    row = _row(build_brief(session), "boundary_roles_valid")
    assert row["status"] == FAIL, row
    assert "farfield" in row["detail"], row


def test_the_boundary_row_does_not_claim_admission_on_a_fail():
    """THE LABEL SAID SOMETHING FALSE ON A FAIL. The one wording asserted admission whatever the
    gate found, so the row above read:

        status fail
        label  "The coolant's ports and the part's outer surface: 2 declared boundaries, each a
                role this conjugate case admits"
        detail "roles not admitted by conjugate_heat_transfer: farfield"

    MEASURED through the real build_checks with cooled_solid True and patches
    [{farfield}, {wall}]. The old row stated a COUNT and could not be false that way; this one
    stated a claim, on exactly the row a cold-plate owner is meant to be able to check. It counts
    both halves now, and the pass wording is unchanged.
    """
    base = {"purpose": "conjugate_heat_transfer", "engine": "cfmesh_conjugate",
            "input_kind": "solid-assembly", "dimensionality": "3D", "cooled_solid": True,
            "requirements_text": "x" * 80, "review_brief_text": "y" * 80, "submitted": True}
    bad = {c["id"]: c for c in build_checks({
        **base, "boundary_assignments": [{"name": "ff", "role": "farfield"},
                                         {"name": "wall", "role": "wall"}]})}
    row = bad["boundary_roles_valid"]
    assert row["status"] == FAIL, row
    assert "admits" not in row["label"], row["label"]
    assert row["label"] == ("The coolant's ports and the part's outer surface: 2 declared "
                           "boundaries, 1 of them a role this conjugate case does not admit")
    assert row["detail"] == "roles not admitted by conjugate_heat_transfer: farfield"
    # ...and every refused boundary is counted, not just the first
    worse = {c["id"]: c for c in build_checks({
        **base, "boundary_assignments": [{"name": "ff", "role": "farfield"},
                                         {"name": "ff2", "role": "farfield"},
                                         {"name": "wall", "role": "wall"}]})}
    assert "3 declared boundaries, 2 of them a role" in worse["boundary_roles_valid"]["label"]
    # ...and a card that passes still says what it said
    good = {c["id"]: c for c in build_checks({
        **base, "boundary_assignments": [{"name": "port_b", "role": "inlet"},
                                         {"name": "port_a", "role": "outlet"},
                                         {"name": "wall", "role": "wall"}]})}
    ok = good["boundary_roles_valid"]
    assert ok["status"] == PASS, ok
    assert ok["label"] == ("The coolant's ports and the part's outer surface: 3 declared "
                           "boundaries, each a role this conjugate case admits")


def test_the_ordinary_boundary_row_is_untouched():
    """The row every CFD and thermal customer reads: unchanged, pass and fail."""
    base = {"purpose": "internal_cfd", "engine": "cfmesh", "input_kind": "solid-body",
            "dimensionality": "3D", "requirements_text": "x" * 80,
            "review_brief_text": "y" * 80, "submitted": True}
    rows = {c["id"]: c for c in build_checks({
        **base, "boundary_assignments": [{"name": "inlet", "role": "inlet"},
                                         {"name": "outlet", "role": "outlet"}]})}
    assert rows["boundary_roles_valid"]["label"] == (
        "2 boundary assignment(s) valid for internal_cfd")
    bad = {c["id"]: c for c in build_checks({
        **base, "boundary_assignments": [{"name": "ff", "role": "farfield"}]})}
    assert bad["boundary_roles_valid"]["label"] == (
        "1 boundary assignment(s) valid for internal_cfd")
    assert bad["boundary_roles_valid"]["status"] == FAIL


def test_the_thermal_models_two_rows_are_untouched():
    """The one working case the brief must not move. `check_mode` is put on the brief by
    `_thermal_view`, which reads the stored geometry check this test has no business writing, so
    the rows are taken straight off the real build_checks with the brief a thermal model makes."""
    base = {"purpose": "conjugate_heat_transfer", "input_kind": "solid-assembly",
            "dimensionality": "3D", "requirements_text": "x" * 80,
            "review_brief_text": "y" * 80, "submitted": True}
    box = {c["id"]: c for c in build_checks({**base, "engine": "snapgrid",
                                             "check_mode": "thermal"})}
    assert "thermal model" in box["engine_compatible"]["label"], box["engine_compatible"]
    assert box["boundary_roles_valid"]["label"] == (
        "The air box's sides are kept as the file has them")
    plate = {c["id"]: c for c in build_checks({
        **base, "engine": "cfmesh_conjugate", "cooled_solid": True,
        "boundary_assignments": [{"name": "port_b", "role": "inlet"},
                                 {"name": "port_a", "role": "outlet"},
                                 {"name": "wall", "role": "wall"}]})}
    assert "thermal model" not in plate["engine_compatible"]["label"]
    assert "air box" not in plate["boundary_roles_valid"]["label"]
