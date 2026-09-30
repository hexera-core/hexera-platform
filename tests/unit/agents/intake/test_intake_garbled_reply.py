# Responsibility: Verify a reply that is not fit to send - the model's planning notes, or a hole where
# a value belongs - is sent back once for a clean rewrite, and that finished engineering prose never is.
# Boundaries: the rule over real replies from the demo and soak transcripts, and the loop policy
# around it; what the model writes instead is the model's.
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from meshpipeline.agents.intake import turn
from meshpipeline.agents.intake.executor import IntakeExecutionState
from meshpipeline.agents.intake.garbled import flaw
from meshpipeline.agents.intake.loop_policy import IntakeLoopPolicy
from meshpipeline.contracts.agent_loop import LoopLimits, LoopTally
from meshpipeline.contracts.engineering_text import plain

_REPLIES = json.loads((Path(__file__).parents[3] / "fixtures" / "intake_garbled_replies.json").read_text(encoding="utf-8"))

# the three replies the demo transcript gate (2026-09-30) found, as the user would have read them
_NOTE = ("For the snappyHexMesh setup, I propose wall-function treatment with y⁺ = 30–300, about Layer? "
         "Need avoid unclear. say 8 prism layers, smooth growth; resolve leading edge, trailing edge, blade tip, "
         "root, and near wake; use patches `blade` (wall) and `farfield` (farfield). Accept this proposal or "
         "specify changes.")
_HOLE = ("I would use wall functions with y⁺ = 30–300, about  layer inflation, and explicit refinement around "
         "the nose, wing and tail leading/trailing edges. Is that setup acceptable, or what should I change?")
_STRAY = ("**Proposal:** wall functions with y⁺ = 30–300 and approximately it 5 prism layers on the shell and "
          "tube walls. Confirm this or specify a resolved-wall target such as y⁺ ≈ 1.")
_CLEAN = ("For the snappyHexMesh setup, I propose wall functions with y⁺ = 30–300 and 8 prism layers on "
          "`blade`, with refinement at the leading and trailing edges - ok, or tell me what differs.")


# ------------------------------------------------------------------------------- the rule ----
@pytest.mark.parametrize("reply", [_NOTE, _HOLE, _STRAY])
def test_the_demo_gates_replies_are_not_fit_to_send(reply):
    assert flaw(reply) is not None


def test_a_note_and_a_hole_are_told_apart():
    assert flaw(_NOTE).kind == "note" and "Need avoid unclear." in flaw(_NOTE).fragment
    assert flaw(_HOLE).kind == "hole" and flaw(_STRAY).kind == "hole"


@pytest.mark.parametrize("reply", _REPLIES["garbled"])
def test_every_garbled_reply_in_the_transcripts_is_caught(reply):
    # as captured, and as the policy reads it: made plain first
    assert flaw(reply) is not None and flaw(plain(reply)) is not None, reply


@pytest.mark.parametrize("reply", _REPLIES["clean"])
def test_no_finished_reply_in_the_transcripts_is_caught(reply):
    # the replies closest to the rules: "need", "wait", "say", "about", tables, spaced columns
    assert flaw(reply) is None and flaw(plain(reply)) is None, (flaw(reply) or flaw(plain(reply)), reply)


@pytest.mark.parametrize("reply", [
    _CLEAN,
    "Need a finer mesh near the wall? Say so and I will tighten the first layer.",
    "Need to change anything? Otherwise I will take air at 15 °C, 40 m/s - ok?",
    "I would use a few prism layers, e.g. say 5 layers at y⁺ = 30–300 - ok?",
    "Wait times depend on the queue; the mesh usually takes 20 minutes.",
    "About 8 prism layers is typical here; about 5 is enough for wall functions.",
    "| Patch  | Type   |\n|--------|--------|\n| inlet  | inlet  |",
    "```\nnSurfaceLayers  5;\nexpansionRatio  1.2;\n```\nThose are the layer settings - ok?",
    "Use `inlet  duct` as written.",
    "The reference length is 4200 mm.  The far field is 5 lengths upstream.",
])
def test_ordinary_engineering_prose_is_left_alone(reply):
    assert flaw(reply) is None, flaw(reply)


# ---------------------------------------------------------------------------- the loop ----
def _round(text):
    return SimpleNamespace(assistant_text=text, finish_reason="stop", input_tokens=1, output_tokens=1)


def _policy(**over):
    st = IntakeExecutionState(session_id="s", owner_id="u", revision="r", user_msg_count=1)
    return IntakeLoopPolicy(exec_state=st, executor=None, **over)


def test_a_garbled_reply_is_sent_back_once_for_a_clean_rewrite():
    p = _policy()
    p.note_round(_round(_NOTE))
    d = p.on_plaintext(LoopTally(rounds=1))
    assert d.complete is False and d.message == turn.GARBLED_NUDGE
    assert p.garbled_replies == 1
    p.note_round(_round(_CLEAN))                          # the rewrite goes to the user
    d = p.on_plaintext(LoopTally(rounds=2))
    assert d.complete is True and d.payload == _CLEAN


def test_a_rewrite_that_is_still_garbled_is_delivered_rather_than_looped():
    p = _policy()
    p.note_round(_round(_HOLE))
    assert p.on_plaintext(LoopTally(rounds=1)).complete is False
    p.note_round(_round(_HOLE))
    d = p.on_plaintext(LoopTally(rounds=2))
    assert d.complete is True and d.payload == _HOLE and p.garbled_replies == 1


def test_no_rewrite_on_the_last_permitted_round():
    # a nudge the budget cannot honour would end the turn with no reply at all
    p = _policy(limits_=LoopLimits(max_rounds=1))
    p.note_round(_round(_STRAY))
    d = p.on_plaintext(LoopTally(rounds=1))
    assert d.complete is True and d.payload == _STRAY and p.garbled_replies == 0


def test_a_clean_reply_completes_the_turn_at_once():
    p = _policy()
    p.note_round(_round(_CLEAN))
    d = p.on_plaintext(LoopTally(rounds=1))
    assert d.complete is True and d.payload == _CLEAN and p.garbled_replies == 0


def test_the_count_reaches_the_run_record_and_the_nudge_is_never_the_users():
    p = _policy()
    p.note_round(_round(_NOTE))
    p.on_plaintext(LoopTally(rounds=1))
    assert p.extension().garbled_replies == 1 and p.extension().sanitized()["garbled_replies"] == 1
    out = turn.serialise_transcript([{"role": "assistant", "content": _NOTE},
                                     {"role": "user", "content": turn.GARBLED_NUDGE}], _CLEAN)
    assert next(e for e in out if e["content"] == turn.GARBLED_NUDGE)["_synthetic"] is True
