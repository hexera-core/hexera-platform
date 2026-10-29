"""Where a port is, is measured - so the model must not be the thing that carries it.

MEASURED LIVE, 2026-10-29, the same STEP file through the same engine on the same path, twice.
The geometry stage measured two 0.8 x 0.8 mm mouths, drew them as numbered stickers, and the user
pressed Proceed on the picture. What then reached the submission:

    run A   patches [{inlet, near_mm [6,5,2], diameter_mm 0.9}, {outlet, near_mm [2,1,2], ...}]
    run B   patches [{inlet}, {outlet}]

Run B's approval payload also had no `port_details_note`, which is the flag the intake prompt asks
the model to set when a port carries details - so the model simply did not copy them that time.

WHAT IT COST. Run B built a correct mesh, 6.5M cells, both regions, and was refused:

    conjugate: the declared ports could not be matched to the mouths by position, so the mesh
               keeps this engine's own port names
    gate patch_contract REJECTED: a boundary did not come out under the name you approved
    route_after_executor: cause 'contract_mismatch', which another attempt cannot change -> END

29 minutes of meshing, discarded, because `coolant_in` is not `inlet`. `mouths.declared_for`
already carries the scar of this exact failure happening once before, and its answer - match by
position - works only while the position survives the trip.

THE RECORD WAS NEVER LOST, only the copy. `api/v1/geometry.py` writes `confirmed.json` with the
full declaration before the session takes it ("the stored copy goes first"), so the measurement is
durable. These tests pin that the submission is refilled from it, at the boundary that already
rewrites declared patch names, and that nothing else about the submission moves.
"""
from __future__ import annotations

import inspect

import pytest

from meshpipeline.application.geometry_confirmation import (
    PORT_DETAIL_FIELDS,
    restore_port_details,
)

#: what the stage measured and the user confirmed, by name
CONFIRMED = {
    "inlet": {"near_mm": [6.0, 5.0, 2.0], "diameter_mm": 0.9, "width_mm": 0.8, "height_mm": 0.8},
    "outlet": {"near_mm": [2.0, 1.0, 2.0], "diameter_mm": 0.9, "width_mm": 0.8, "height_mm": 0.8},
}

#: what the model submitted on the run that failed
BARE = [{"name": "inlet", "type": "inlet"}, {"name": "outlet", "type": "outlet"},
        {"name": "wall", "type": "wall"}]


def test_a_port_that_lost_its_location_gets_it_back():
    rows, restored = restore_port_details(BARE, CONFIRMED)
    by = {r["name"]: r for r in rows}
    assert by["inlet"]["near_mm"] == [6.0, 5.0, 2.0]
    assert by["outlet"]["near_mm"] == [2.0, 1.0, 2.0]
    assert sorted(restored) == ["inlet", "outlet"]


def test_every_field_the_approval_is_fingerprinted_on_is_restored():
    """`admission_token.approved_intent_canonical` fingerprints a `port_declaration` built from
    exactly these fields. One of them missing is one the engine cannot use and the approval cannot
    attest."""
    rows, _ = restore_port_details(BARE, CONFIRMED)
    inlet = next(r for r in rows if r["name"] == "inlet")
    for k in ("near_mm", "diameter_mm", "width_mm", "height_mm"):
        assert k in inlet
    assert set(CONFIRMED["inlet"]) <= set(PORT_DETAIL_FIELDS)


def test_a_value_the_model_stated_is_never_overwritten():
    """A user who corrects a bore in the conversation is still right. This fills what is ABSENT."""
    said = [{"name": "inlet", "type": "inlet", "diameter_mm": 1.4}]
    rows, restored = restore_port_details(said, CONFIRMED)
    assert rows[0]["diameter_mm"] == 1.4, "the conversation lost to a stored measurement"
    assert rows[0]["near_mm"] == [6.0, 5.0, 2.0], "what was missing was still filled"
    assert restored == ["inlet"]


def test_nothing_is_added_removed_or_renamed():
    rows, _ = restore_port_details(BARE, CONFIRMED)
    assert [r["name"] for r in rows] == [p["name"] for p in BARE]
    assert [r["type"] for r in rows] == [p["type"] for p in BARE]
    assert len(rows) == len(BARE)


def test_a_wall_gets_no_port_details():
    """Only an inlet or an outlet is a port. A wall with a location is a declaration nobody made."""
    rows, _ = restore_port_details(BARE, {**CONFIRMED, "wall": {"near_mm": [0, 0, 0]}})
    wall = next(r for r in rows if r["name"] == "wall")
    assert wall == {"name": "wall", "type": "wall"}


def test_nothing_confirmed_restores_nothing():
    rows, restored = restore_port_details(BARE, {})
    assert rows == BARE and restored == []


def test_a_port_with_a_name_nobody_confirmed_is_left_alone():
    """A port the user added in the conversation, after the stage. It has no measurement to put
    back and inventing one would be worse than the gap."""
    rows, restored = restore_port_details([{"name": "drain", "type": "outlet"}], CONFIRMED)
    assert rows == [{"name": "drain", "type": "outlet"}]
    assert restored == []


@pytest.mark.parametrize("junk", [None, "", 7, [1, 2]])
def test_a_patch_that_is_not_a_dict_passes_through(junk):
    rows, restored = restore_port_details([junk, {"name": "inlet", "type": "inlet"}], CONFIRMED)
    assert rows[0] is junk and restored == ["inlet"]


#   -- the submission keeps them too -------------------------------------------------------------

def test_the_normalised_submission_keeps_what_a_port_declares():
    """This used to rebuild every patch as exactly {name, type}, so the SESSION's copy of the
    declaration was empty while the approval record's was not - and the session's is the one a
    later turn, a unit change or a re-submission reads back."""
    from meshpipeline.agents.intake.validation import normalise_submission

    got = normalise_submission({"purpose": "conjugate_heat_transfer", "patches": [
        {"name": "inlet", "type": "inlet", "near_mm": [6, 5, 2], "diameter_mm": 0.9},
        {"name": "wall", "type": "wall"}]}).intake_patches
    assert got[0] == {"name": "inlet", "type": "inlet", "near_mm": [6, 5, 2], "diameter_mm": 0.9}
    assert got[1] == {"name": "wall", "type": "wall"}


def test_the_normalised_submission_carries_nothing_a_declaration_is_not_made_of():
    """The fields are an allow-list, so a model that invents a key does not get it stored."""
    from meshpipeline.agents.intake.validation import normalise_submission

    got = normalise_submission({"purpose": "conjugate_heat_transfer", "patches": [
        {"name": "inlet", "type": "inlet", "near_mm": [6, 5, 2],
         "temperature_K": 300, "note": "hot"}]}).intake_patches
    assert got == [{"name": "inlet", "type": "inlet", "near_mm": [6, 5, 2]}]


#   -- and it happens where it has to ------------------------------------------------------------

def test_the_details_are_restored_before_anything_sees_the_submission():
    """IT HAS TO BE THIS BOUNDARY. `approved_intent_canonical` fingerprints a `port_declaration`
    carrying near_mm and the sizes, and `assert_payload_matches_approval` refuses a dispatch whose
    intent fingerprint has moved - so filling these in at dispatch would be refused as drift. The
    executor's own comment already states the rule for the rename it does here: "before the
    preview, the preview token, the approved snapshot, the builder payload and the manifest check
    see any of it, so none of them can disagree"."""
    from meshpipeline.agents.intake.executor import IntakeToolExecutor

    src = inspect.getsource(IntakeToolExecutor.run)
    at_rename = src.index("_normalize_declared_patches")
    at_restore = src.index("_with_confirmed_port_details")
    at_handler = src.index("handler = getattr(self")
    assert at_rename < at_restore < at_handler, (
        "the measured fields must be back before any handler, preview or approval sees them")


def test_the_restoration_is_said_out_loud_to_the_model():
    """Every other change made at this boundary is reported in the tool result; a model working
    from a declaration it cannot see is the thing that started this."""
    from meshpipeline.agents.intake.executor import IntakeToolExecutor

    src = inspect.getsource(IntakeToolExecutor.run)
    assert "[SYSTEM] The size and location the user confirmed" in src
    assert "not new input" in src


def test_an_unreadable_confirmation_never_costs_the_submission():
    """No store, no confirmation, a confirmation this cannot parse: the submission stands exactly
    as it arrived. A restoration is an improvement, never a precondition."""
    from meshpipeline.agents.intake.executor import IntakeToolExecutor

    src = inspect.getsource(IntakeToolExecutor._with_confirmed_port_details)
    assert "except Exception" in src
    assert "return list(patches or []), []" in src
