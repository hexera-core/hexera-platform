# Responsibility: Verify the same question is never put to the user twice - by the loop, not only by the prompt.
# Boundaries: the repeat guard and the prompt rules it backs; what a question's answer means is the model's reading.
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import meshpipeline.agents.intake.agent as intake
from meshpipeline.agents.intake import turn
from meshpipeline.agents.intake.executor import IntakeExecutionState
from meshpipeline.agents.intake.loop_policy import IntakeLoopPolicy, repeats_a_question
from meshpipeline.contracts.agent_loop import LoopTally

# The question a real session asked four times in a row because the user's replies did not match
# what the model wanted to hear.
_Q = ("Should I treat the pipe wall as hydraulically smooth (no roughness), with a first-layer "
      "thickness sized for y+ 30-300 and 5 prism layers - ok?")
_Q_REWORDED = ("To confirm: hydraulically smooth pipe wall, first-layer thickness for y+ 30-300, "
               "5 prism layers. Is that right?")
_NEXT = "Noted. Fluid and speed? I would go with water at 2 m/s - ok, or tell me what differs."
_MOVED_ON = ("Fine - I will take hydraulically smooth with y+ 30-300 as the assumption. Fluid and "
             "speed? Water at 2 m/s - ok?")


# the repeat rule

def test_the_same_question_again_is_a_repeat():
    assert repeats_a_question(_Q, (_Q,))


def test_a_rewording_of_the_same_question_is_a_repeat():
    assert repeats_a_question(_Q_REWORDED, (_Q,))


def test_a_different_question_is_not():
    assert not repeats_a_question(_NEXT, (_Q,))


def test_taking_the_proposal_and_moving_on_is_not():
    # The reply the rule wants: the old question's values stated as an assumption, and the next
    # question asked. It shares words with the old question and is not a repeat of it.
    assert not repeats_a_question(_MOVED_ON, (_Q,))


def test_a_statement_is_never_a_repeat():
    assert not repeats_a_question("Noted - the wall is hydraulically smooth, y+ 30-300.", (_Q,))


def test_nothing_asked_before_is_never_a_repeat():
    assert not repeats_a_question(_Q, ())
    assert not repeats_a_question(_Q, ("Which engine do you use?",))


def test_a_short_question_repeated_word_for_word_is_a_repeat():
    assert repeats_a_question("Fluid and speed?", ("Fluid and speed?",))
    assert repeats_a_question("Which engine?", ("Which engine?",))


def test_a_short_question_is_not_matched_on_a_rephrase_or_on_nothing():
    # Too few words to judge a rephrase on; and a question with no content words ("Ok?") is not
    # a question the guard can recognise at all.
    assert not repeats_a_question("Which engine?", ("Which mesher?",))
    assert not repeats_a_question("Ok?", ("Ok?",))


# the loop: sent back once, then delivered

def _round(text):
    return SimpleNamespace(assistant_text=text, finish_reason="stop", input_tokens=1,
                           output_tokens=1)


def _policy(prior):
    st = IntakeExecutionState(session_id="s", owner_id="u", revision="r", user_msg_count=1)
    return IntakeLoopPolicy(exec_state=st, executor=None, prior_questions=prior)


def test_a_repeated_question_is_sent_back_once_with_the_rule():
    p = _policy((_Q,))
    p.note_round(_round(_Q_REWORDED))
    d = p.on_plaintext(LoopTally(rounds=1))
    assert d.complete is False and d.message == turn.REPEAT_NUDGE
    assert "already asked" in d.message and "proposal" in d.message
    assert p.repeated_questions == 1


def test_the_second_reply_is_delivered_whatever_it_says():
    # Once per turn. A model that insists still ends the turn: the user is never left waiting on
    # a loop between the application and the model.
    p = _policy((_Q,))
    p.note_round(_round(_Q))
    assert p.on_plaintext(LoopTally(rounds=1)).complete is False
    p.note_round(_round(_Q))
    d = p.on_plaintext(LoopTally(rounds=2))
    assert d.complete is True and d.payload == _Q
    assert p.repeated_questions == 1


def test_no_nudge_on_the_last_permitted_round():
    # A nudge the budget cannot honour would end the turn with no reply at all. On the last round
    # the repeat is delivered rather than sent back into a round that cannot start.
    from meshpipeline.contracts.agent_loop import LoopLimits

    st = IntakeExecutionState(session_id="s", owner_id="u", revision="r", user_msg_count=1)
    p = IntakeLoopPolicy(exec_state=st, executor=None, prior_questions=(_Q,),
                         limits_=LoopLimits(max_rounds=1))
    p.note_round(_round(_Q))
    d = p.on_plaintext(LoopTally(rounds=1))          # the round just taken was the only one
    assert d.complete is True and d.payload == _Q and p.repeated_questions == 0

    p2 = IntakeLoopPolicy(exec_state=st, executor=None, prior_questions=(_Q,),
                          limits_=LoopLimits(max_rounds=2))
    p2.note_round(_round(_Q))
    assert p2.on_plaintext(LoopTally(rounds=1)).complete is False   # one round is left


def test_a_fresh_question_completes_the_turn_at_once():
    p = _policy((_Q,))
    p.note_round(_round(_NEXT))
    d = p.on_plaintext(LoopTally(rounds=1))
    assert d.complete is True and d.payload == _NEXT and p.repeated_questions == 0


def test_the_count_reaches_the_run_record():
    p = _policy((_Q,))
    p.note_round(_round(_Q))
    p.on_plaintext(LoopTally(rounds=1))
    assert p.extension().repeated_questions == 1
    assert p.extension().sanitized()["repeated_questions"] == 1


# the transcript never takes the application's words for the user's

def test_the_repeat_nudge_is_serialised_as_synthetic():
    out = turn.serialise_transcript(
        [{"role": "assistant", "content": _Q, "tool_calls": []},
         {"role": "user", "content": turn.REPEAT_NUDGE}], _NEXT)
    nudge = next(e for e in out if e["content"] == turn.REPEAT_NUDGE)
    assert nudge["_synthetic"] is True
    assert "_synthetic" not in next(e for e in out if e["content"] == _NEXT)


def test_the_budget_nudge_in_the_model_messages_is_serialised_as_synthetic_too():
    llm, _ = turn.apply_budget_nudge([{"role": "user", "content": "hi"}], [])
    out = turn.serialise_transcript(llm, "")
    assert out[-1]["_synthetic"] is True and "_synthetic" not in out[0]


def test_prior_questions_are_the_assistants_own_lines_only():
    _, nudged = turn.apply_budget_nudge([], [
        {"role": "user", "content": "hi"}, {"role": "assistant", "content": _Q},
        {"role": "user", "content": "hmm"}])
    assert turn.prior_assistant_texts(nudged) == (_Q,)


# the node, end to end

def _tc(name, args):
    return SimpleNamespace(id="t", function=SimpleNamespace(name=name, arguments=args))


def _resp(tool_calls=None, content=""):
    from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest
    return ModelRoundResult(
        tool_calls=tuple(ToolCallRequest(id=tc.id, name=tc.function.name,
                                         arguments=tc.function.arguments)
                         for tc in (tool_calls or [])),
        assistant_text=content,
        finish_reason="tool_calls" if tool_calls else "stop")


def _run(state, responses):
    import meshpipeline.contracts.model_inference as llm
    it = iter(responses)
    calls: list = []

    async def _call(**kw):
        calls.append([dict(m) for m in (kw.get("messages") or []) if isinstance(m, dict)])
        return next(it)
    with patch.object(llm, "call_intake_model", _call):
        return asyncio.run(intake.node_intake(state)), calls


_STATE = {"job_id": "j", "session_id": "s", "user_id": "u",
          "messages": [{"role": "user", "content": "internal flow in a pipe, snappy"},
                       {"role": "assistant", "content": _Q},
                       {"role": "user", "content": "the pipe is steel, does that matter?"}]}


def test_asking_again_is_sent_back_and_the_moved_on_reply_is_delivered():
    out, calls = _run(dict(_STATE), [_resp(content=_Q_REWORDED), _resp(content=_MOVED_ON)])
    assert len(calls) == 2, "the repeat was not sent back"
    assert calls[1][-1] == {"role": "user", "content": turn.REPEAT_NUDGE}
    assert out["messages"][-1]["content"] == _MOVED_ON
    assert out.get("dispatch_confirmed") is False


def test_a_reply_that_moves_on_is_delivered_in_one_round():
    out, calls = _run(dict(_STATE), [_resp(content=_MOVED_ON)])
    assert len(calls) == 1
    assert out["messages"][-1]["content"] == _MOVED_ON


def test_a_repeat_on_the_last_round_is_delivered_not_silence(monkeypatch):
    monkeypatch.setattr(intake.icfg, "INTAKE_MAX_ROUNDS", 1)
    out, calls = _run(dict(_STATE), [_resp(content=_Q_REWORDED), _resp(content=_MOVED_ON)])
    assert len(calls) == 1
    assert out["messages"][-1]["content"] == _Q_REWORDED, "the user got no reply at all"


def test_the_nudge_is_not_persisted_as_a_conversation_message():
    out, _ = _run(dict(_STATE), [_resp(content=_Q), _resp(content=_MOVED_ON)])
    assert [m["role"] for m in out["messages"]] == ["assistant"]
    assert json.dumps(out["messages"]).count(turn.REPEAT_NUDGE) == 0


# the prompt rules the guard backs

def test_the_propose_first_block_reads_a_plain_yes_and_moves_on_after_one_unclear_reply():
    block = intake._block_propose_first()
    for accepted in ("'ok'", "'yes'", "'sensible default'", "'I do not know'"):
        assert accepted in block, accepted
    low = block.lower()
    assert "never ask the same question twice" in low
    assert "one answer" in low and "move on" in low
    for courtesy in ("y+", "first-layer thickness", "layer count", "patch names"):
        assert courtesy in low, courtesy
    assert "never a reason to hold a submission" in low


def test_the_prompt_blocks_are_the_registered_four():
    assert [b[0] for b in intake.INTAKE_PROMPT_BLOCKS] == [
        "quality_criteria", "engine_first", "geometry_check", "propose_first"]
    assert "propose_first" in {b[0] for b in intake.INTAKE_PROMPT_BLOCKS}
    assert intake._block_propose_first() in intake.compose_intake_system()
