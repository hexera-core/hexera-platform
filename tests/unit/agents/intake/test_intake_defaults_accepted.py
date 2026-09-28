# Responsibility: Verify "I do not know, use a default" is honoured and a corrected unit re-asks nothing the check settled.
# Boundaries: two transcript shapes from the console path, replayed against the node; the repeat rule itself is test_intake_no_nagging.
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

import meshpipeline.agents.intake.agent as intake
from meshpipeline.agents.intake import turn
from meshpipeline.agents.intake.loop_policy import repeats_a_question


def _resp(content):
    from meshpipeline.contracts.model_inference import ModelRoundResult
    return ModelRoundResult(tool_calls=(), assistant_text=content, finish_reason="stop")


def _run(state, responses):
    import meshpipeline.contracts.model_inference as llm
    it = iter(responses)
    calls: list = []

    async def _call(**kw):
        calls.append([dict(m) for m in (kw.get("messages") or []) if isinstance(m, dict)])
        return next(it)
    with patch.object(llm, "call_intake_model", _call):
        return asyncio.run(intake.node_intake(state)), calls


def _state(*messages):
    return {"job_id": "j", "session_id": "s", "user_id": "u", "messages": list(messages)}


# 1. the mixing tee: the check confirmed 1 inlet + 2 outlets; the user said two inlets feed one
#    outlet; the intake asked which outlet is the second inlet NINE times, and told four "I do
#    not know, use a sensible default and continue" replies that the role "cannot be selected by
#    default".

_TEE_CHECK = (
    "GEOMETRY CHECK (confirmed by the user): the file is the fluid volume itself, input_kind "
    "fluid-domain; the fluid flows through it. Openings: inlet_1 (inlet), 40 mm across at "
    "(0, 0, 0) mm; outlet_1 (outlet), 40 mm across at (100, 0, 0) mm; outlet_2 (outlet), 40 mm "
    "across at (50, 80, 0) mm. A point inside the flow: (50, 10, 0) mm. Part size: 100 x 80 x 40 mm."
)
_TEE_Q = "Which opening should be the second inlet: outlet_1 or outlet_2?"
_TEE_DEFER = "I do not know, use a sensible default and continue"
_TEE_MOVED_ON = ("I will take outlet_1 as the second inlet (assumed, not stated by you). Fluid "
                 "and speed? I would go with water at 1 m/s - ok?")


def _tee_state(*extra):
    return _state({"role": "user", "content": "internal CFD of a mixing tee, snappy"},
                  {"role": "assistant", "content": _TEE_CHECK},
                  {"role": "user", "content": "actually it is two inlets feeding one outlet"},
                  {"role": "assistant", "content": _TEE_Q},
                  *extra)


@pytest.mark.parametrize("said", [
    _TEE_DEFER, "I don't know", "no idea, you decide", "use a sensible default",
    "whatever is standard", "up to you", "I do not know. Use a default and continue.",
])
def test_handing_the_question_back_is_recognised(said):
    assert turn.defers_to_default(said) is True


@pytest.mark.parametrize("said", [
    "outlet_1", "yes", "the file is in metres", "make outlet_2 the inlet and continue",
    "what does inlet mean here?", "not sure the inlet is 40 mm", "",
])
def test_an_answer_or_a_question_is_not_a_deferral(said):
    assert turn.defers_to_default(said) is False


def test_a_deferral_puts_the_take_the_default_rule_in_front_of_the_model():
    out, calls = _run(_tee_state({"role": "user", "content": _TEE_DEFER}),
                      [_resp(_TEE_MOVED_ON)])
    assert len(calls) == 1
    assert calls[0][-1] == {"role": "user", "content": turn.DEFAULT_NUDGE}
    assert calls[0][-2]["content"] == _TEE_DEFER, "the user's own words come first"
    assert out["messages"][-1]["content"] == _TEE_MOVED_ON


def test_the_rule_says_what_to_take_and_keeps_the_two_exceptions():
    n = turn.DEFAULT_NUDGE
    assert "Do not ask it again" in n and "cannot be defaulted" in n
    assert "first one you listed" in n and "assumed, not stated by the user" in n
    assert "ENGINE is proposed and confirmed, never defaulted" in n and "UNIT is asked" in n


def test_the_deferral_line_is_the_applications_not_the_users():
    # It must neither count as a user message (the approval's expected confirmation would move)
    # nor read as the user's words in the corpus.
    llm, state = turn.apply_default_nudge([], _tee_state({"role": "user", "content": _TEE_DEFER})["messages"])
    ctx = turn.hydrate({"job_id": "j", "session_id": "s", "user_id": "u", "intake_gate": None}, state)
    assert ctx.user_msg_count == 3 and ctx.latest_user_msg == _TEE_DEFER
    assert turn.serialise_transcript(llm, "")[-1]["_synthetic"] is True
    assert turn.prior_assistant_texts(state) == (_TEE_CHECK, _TEE_Q)


def test_asking_a_tenth_time_anyway_is_sent_back_and_the_default_is_taken():
    # The model ignores the rule and asks the same question again: the loop sends it back once,
    # and the reply that takes the default is what the user reads.
    out, calls = _run(_tee_state({"role": "user", "content": _TEE_DEFER}),
                      [_resp(_TEE_Q), _resp(_TEE_MOVED_ON)])
    assert len(calls) == 2 and calls[1][-1]["content"] == turn.REPEAT_NUDGE
    assert out["messages"][-1]["content"] == _TEE_MOVED_ON


def test_taking_the_default_and_moving_on_is_not_a_repeat_of_the_question():
    assert not repeats_a_question(_TEE_MOVED_ON, (_TEE_CHECK, _TEE_Q))
    assert repeats_a_question(_TEE_Q, (_TEE_CHECK, _TEE_Q))


# 2. the turbine: the check confirmed flow along +x on a 7 x 7 x 117 mm part; the user said the
#    file is in metres; the intake declared a "conflict" and demanded the axis EIGHT times,
#    refusing "yes, go ahead" and "use a sensible default".

_TURBINE_CHECK = (
    "GEOMETRY CHECK (confirmed by the user): the file is a solid body (a wind turbine), "
    "input_kind solid-body; the fluid flows around it. No openings: the fluid flows around the "
    "whole body. The fluid travels along +x. Reference length: 117 mm along the flow. Far-field "
    "margins in reference lengths: 5 upstream, 10 downstream, 5 to each side, 5 above. The part "
    "is free in the flow; every far-field face is open. Part size: 7 x 7 x 117 mm."
)
_AXIS_Q = "Which axis does the flow travel along: +x, -x, +y, -y, +z or -z?"
_RECOMPUTED = ("Taking the flow along +x from the check. In metres the part is 7 x 7 x 117 m, so "
               "the reference length is 117 m and the far-field margins stay 5 upstream, 10 "
               "downstream, 5 to each side and 5 above in reference lengths - ok, or tell me "
               "what differs.")


def _turbine_state(*extra):
    return _state({"role": "user", "content": "external aero on a wind turbine, snappy"},
                  {"role": "assistant", "content": _TURBINE_CHECK},
                  {"role": "user", "content": "the file is in metres, not millimetres"},
                  *extra)


def test_the_axis_the_check_settled_is_never_asked_twice():
    state = _turbine_state({"role": "assistant", "content": _AXIS_Q},
                           {"role": "user", "content": "yes, go ahead"})
    out, calls = _run(state, [_resp(_AXIS_Q), _resp(_RECOMPUTED)])
    assert len(calls) == 2 and calls[1][-1]["content"] == turn.REPEAT_NUDGE
    assert out["messages"][-1]["content"] == _RECOMPUTED


def test_use_a_sensible_default_to_a_re_asked_axis_is_honoured_in_code():
    state = _turbine_state({"role": "assistant", "content": _AXIS_Q},
                           {"role": "user", "content": "use a sensible default"})
    out, calls = _run(state, [_resp(_RECOMPUTED)])
    assert len(calls) == 1 and calls[0][-1]["content"] == turn.DEFAULT_NUDGE
    assert out["messages"][-1]["content"] == _RECOMPUTED


def test_the_recomputed_proposal_is_not_a_repeat_of_the_axis_question():
    assert not repeats_a_question(_RECOMPUTED, (_TURBINE_CHECK, _AXIS_Q))


# the prompt rules the two shapes need

def test_the_propose_first_block_says_a_value_left_to_the_model_is_chosen_not_re_asked():
    block = intake._block_propose_first()
    assert "A VALUE THE USER LEAVES TO YOU is yours to choose" in block
    assert "second inlet" in block and "first candidate you listed" in block
    assert "cannot be defaulted" in block
    assert "ENGINE is proposed and confirmed, never defaulted" in block
    assert "UNIT is asked, never guessed" in block


def test_the_geometry_check_block_says_a_unit_change_re_asks_nothing_settled():
    block = intake._block_geometry_check()
    assert "different unit" in block and "not a conflict" in block
    assert "never a reason to ask the flow axis" in block
    assert "convert the reference length" in block


def test_the_block_registry_is_unchanged():
    assert [b[0] for b in intake.INTAKE_PROMPT_BLOCKS] == [
        "quality_criteria", "engine_first", "geometry_check", "propose_first"]
