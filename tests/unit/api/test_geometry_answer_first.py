# Responsibility: Verify the answer-first geometry check's rules - when a chat turn holds for the
# naming, what the user's words become, what the confirm form and the registry agree on, and how
# a body in a flow is declared to the intake.
# Boundaries: pure functions and pydantic models; no store, no database, no model.
from __future__ import annotations

from pathlib import Path

from meshpipeline.api.v1.geometry import (
    CONTINUE_TEXT,
    DRAWING_MARK,
    HOLD_REPLY,
    ConfirmedOpening,
    ConfirmIn,
    confirmation_message,
    patches_from,
    purpose_from,
    should_hold,
)
from meshpipeline.application import geometry_hold as gf_hold
from meshpipeline.contracts import geometry_fields as gf


# ---------------------------------------------------------------------------- the hold ----
def test_the_first_answer_holds_while_the_check_is_pending_or_scouted():
    assert should_hold({"status": "pending"}, None, None) is True
    assert should_hold({"status": "scouted"}, None, None) is True


def test_nothing_holds_once_the_naming_was_asked_or_the_check_is_done():
    assert should_hold({"status": "scouted"}, {"requested_at": 1}, None) is False
    assert should_hold({"status": "scouted"}, None, {"confirmed_at": 1}) is False
    assert should_hold({"status": "ready", "named": True}, None, None) is False
    assert should_hold({"status": "failed"}, None, None) is False
    assert should_hold({"status": "unsupported"}, None, None) is False
    assert should_hold(None, None, None) is False


def test_the_users_words_are_every_user_message_so_far():
    msgs = [{"role": "assistant", "content": "Geometry received. What are you meshing this for?"},
            {"role": "user", "content": "  water through this elbow, it's the pipe wall "},
            {"role": "assistant", "content": "What unit is the file in?"},
            {"role": "user", "content": "millimetres"}]
    assert purpose_from(msgs) == "water through this elbow, it's the pipe wall\nmillimetres"
    assert purpose_from([]) == ""


def test_the_holding_line_is_marked_so_the_intake_never_reads_it_as_declared():
    assert HOLD_REPLY.startswith(DRAWING_MARK)
    assert "Proceed" in HOLD_REPLY
    assert CONTINUE_TEXT.lower().startswith("i confirmed the geometry check")


# ------------------------------------------------------------------------ the registry ----
def test_every_confirmable_field_in_the_registry_is_on_the_confirm_model():
    on_model = set(ConfirmIn.model_fields)
    for f in gf.FIELDS:
        assert f.key in on_model, f"registry field {f.key} has no place on ConfirmIn"


def test_the_form_spec_carries_what_the_console_needs():
    spec = gf.form_spec()
    keys = [s["key"] for s in spec]
    assert keys == list(gf.FIELD_KEYS)
    axis = next(s for s in spec if s["key"] == "flow_axis")
    assert axis["applies"] == "external" and ["unknown", "not sure"] in axis["options"]


def test_external_defaults_follow_the_model_when_it_chose_an_axis_and_the_code_otherwise():
    facts = {"size_mm": [4200.0, 1800.0, 1400.0]}
    chosen = gf.external_defaults(facts, "-x")
    assert chosen["flow_axis"] == "-x" and chosen["flow_axis_guessed"] is False
    assert chosen["reference_length_mm"] == 4200.0
    guessed = gf.external_defaults(facts, "unknown")
    assert guessed["flow_axis"] == "+x" and guessed["flow_axis_guessed"] is True
    assert guessed["extents"] == gf.DEFAULT_EXTENTS and guessed["grounded"] is False
    tall = gf.external_defaults({"size_mm": [10.0, 300.0, 900.0], "grounded": True}, None)
    assert tall["flow_axis"] == "+y" and tall["reference_length_mm"] == 300.0 and tall["grounded"] is True


# ------------------------------------------------------------------- the declaration ----
def test_a_body_in_a_flow_declares_its_axis_lengths_and_ground():
    body = ConfirmIn(input_kind="solid-body", flow="external", part="a car", flow_axis="-x",
                     reference_length_mm=4200.0,
                     extents={"upstream": 5, "downstream": 12, "lateral": 5, "vertical": 5}, grounded=True)
    m = confirmation_message(body)
    assert m.startswith("GEOMETRY CHECK (confirmed by the user):")
    assert "flows around it" in m and "The fluid travels along -x." in m
    assert "Reference length: 4200 mm" in m
    assert "5 upstream, 12 downstream, 5 to each side, 5 above" in m
    assert "stands on the ground" in m
    assert patches_from(body) == []        # the far field's faces are the builder's to name


def test_an_unsettled_axis_is_left_for_the_intake_to_ask():
    body = ConfirmIn(input_kind="solid-body", flow="external", flow_axis="unknown")
    assert "ask for it" in confirmation_message(body)


def test_a_sticker_that_is_not_an_opening_is_no_patch():
    body = ConfirmIn(input_kind="body-surface", flow="internal", openings=[
        ConfirmedOpening(id=1, name="inlet", role="inlet", diameter_mm=100.0),
        ConfirmedOpening(id=2, name="outlet", role="outlet", diameter_mm=100.0),
        ConfirmedOpening(id=3, name="bolt_hole", role="not_an_opening", diameter_mm=8.0)])
    assert [p["name"] for p in patches_from(body)] == ["inlet", "outlet", "wall"]
    m = confirmation_message(body)
    assert "Sticker 3: not an opening" in m and "bolt_hole" not in m.split("Sticker 3")[1]


def test_an_internal_part_with_no_confirmed_port_asks_instead_of_contradicting_itself():
    body = ConfirmIn(input_kind="body-surface", flow="internal", openings=[
        ConfirmedOpening(id=1, name="hole", role="not_an_opening", diameter_mm=8.0)])
    m = confirmation_message(body)
    assert "flows through it" in m
    assert "flows around" not in m
    assert "ask the user where the fluid enters and leaves" in m and "every sticker was marked" in m
    assert patches_from(body) == []          # no ports, so no wall either


def test_far_field_margins_must_be_positive():
    import pytest as _pytest
    from pydantic import ValidationError
    with _pytest.raises(ValidationError):
        ConfirmIn(input_kind="solid-body", flow="external", flow_axis="+x",
                  extents={"upstream": 0, "downstream": 10, "lateral": 5, "vertical": 5})
    with _pytest.raises(ValidationError):
        ConfirmIn(input_kind="solid-body", flow="external", flow_axis="+x", extents={"sideways": 5})
    ok = ConfirmIn(input_kind="solid-body", flow="external", flow_axis="+x",
                   extents={"upstream": 3, "downstream": 10.5, "lateral": 5, "vertical": 5})
    assert ok.extents == {"upstream": 3.0, "downstream": 10.5, "lateral": 5.0, "vertical": 5.0}


# ---------------------------------------------------------------------- queue, wait, or not ----
def test_a_message_during_the_drawing_waits_and_one_after_the_grace_does_not():
    requested = {"requested_at": 1000.0}
    assert gf_hold.hold_decision({"status": "scouted"}, None, None, now=1000.0) == "queue"
    assert gf_hold.hold_decision({"status": "scouted"}, requested, None, now=1010.0) == "wait"
    assert gf_hold.hold_decision({"status": "pending"}, requested, None, now=1000.0 + gf_hold.WAIT_GRACE_S) == "wait"
    assert gf_hold.hold_decision({"status": "scouted"}, requested, None, now=1000.0 + gf_hold.WAIT_GRACE_S + 1) is None
    assert gf_hold.hold_decision({"status": "ready", "named": True}, requested, None, now=1010.0) is None
    assert gf_hold.hold_decision({"status": "failed"}, requested, None, now=1010.0) is None
    assert gf_hold.hold_decision({"status": "scouted"}, requested, {"confirmed_at": 1}, now=1010.0) is None
    assert gf_hold.WAIT_REPLY.startswith(DRAWING_MARK) and "still drawing" in gf_hold.WAIT_REPLY


def test_the_console_strips_the_same_marks_the_api_writes():
    js = (Path(__file__).resolve().parents[3] / "ui" / "js" / "render" / "geometry_form.js").read_text()
    assert f'DRAWING_MARK = "{DRAWING_MARK}"' in js
    assert 'CONFIRMED_MARK = "GEOMETRY CHECK (confirmed by the user):"' in js
