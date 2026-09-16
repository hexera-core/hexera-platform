# tests/unit/infra/test_responses_normalize.py
# Responsibility: Verify a Responses payload becomes the one result type every role shares.
# Boundaries: normalisation only; no network.
from __future__ import annotations

from types import SimpleNamespace

from meshpipeline.adapters.model_inference.protocols import responses_normalize as rn
from meshpipeline.contracts.model_routing import RouteTarget

TARGET = RouteTarget(provider="openai", model="gpt-5.6-luna", account="default",
                     circuit_group="openai")


def _resp(output, usage=None):
    return SimpleNamespace(output=output, usage=usage, status="completed")


USAGE = {"input_tokens": 7,
         "input_tokens_details": {"cached_tokens": 3, "cache_write_tokens": 0},
         "output_tokens": 11,
         "output_tokens_details": {"reasoning_tokens": 5},
         "total_tokens": 18}


def test_usage_maps_onto_the_neutral_shape():
    u = rn.usage_from(_resp([], USAGE))
    assert (u.input_tokens, u.cached_input_tokens, u.output_tokens) == (7, 3, 11)


def test_text_output_becomes_assistant_text():
    r = rn.normalize(_resp([{"type": "message", "content": [
        {"type": "output_text", "text": "hello"}]}], USAGE), TARGET, 1)
    assert r.assistant_text == "hello"
    assert r.reasoning_tokens == 5


def test_a_function_call_item_becomes_a_tool_call_request():
    r = rn.normalize(_resp([
        {"type": "reasoning", "summary": []},
        {"type": "function_call", "call_id": "call_9", "name": "get_x",
         "arguments": '{"a":"1"}'},
    ], USAGE), TARGET, 1)
    assert [(c.id, c.name, c.arguments) for c in r.tool_calls] == \
           [("call_9", "get_x", '{"a":"1"}')]


def test_absent_usage_reports_zero_rather_than_inventing():
    r = rn.normalize(_resp([], None), TARGET, 1)
    assert (r.input_tokens, r.output_tokens, r.reasoning_tokens) == (0, 0, 0)


def test_reasoning_tokens_zero_means_not_reported_not_output_tokens():
    # ModelRoundResult's own docstring: zero means NOT REPORTED. Substituting output_tokens
    # would put an invented number in front of an operator.
    u = dict(USAGE, output_tokens_details={})
    r = rn.normalize(_resp([], u), TARGET, 1)
    assert r.reasoning_tokens == 0 and r.output_tokens == 11


# --- finish_reason: the Responses `status` translated into the vocabulary consumers read ------
# agents/builder/loop.py:91 branches on `finish_reason == "length"` to drive truncation recovery.
# Responses says "incomplete" on a different field, so without translation that branch is dead
# code here and the builder silently loses the tail of every truncated round.

def _truncated(reason="max_output_tokens", output=None):
    return SimpleNamespace(
        output=output if output is not None else [{"type": "reasoning", "summary": []}],
        usage=USAGE, status="incomplete",
        incomplete_details={"reason": reason})


def test_a_max_output_tokens_truncation_is_reported_as_length_not_incomplete():
    # Probed 2026-09-15 at max_output_tokens=16: status="incomplete",
    # incomplete_details.reason="max_output_tokens", output=['reasoning'], real usage.
    r = rn.normalize(_truncated(), TARGET, 1)
    assert r.finish_reason == "length", "builder/loop.py:91 reads exactly this word"
    assert r.output_tokens == 11, "a truncated round still reports what it cost"


def test_an_unrecognised_incomplete_reason_still_reports_truncation():
    # "stopped early for a reason we do not have a word for" is still stopped early. Reporting
    # "stop" would be the failure this mapping exists to prevent.
    assert rn.normalize(_truncated(reason=""), TARGET, 1).finish_reason == "length"
    assert rn.normalize(SimpleNamespace(output=[], usage=None, status="incomplete"),
                        TARGET, 1).finish_reason == "length"


def test_a_content_filtered_incomplete_keeps_its_own_word():
    assert rn.normalize(_truncated(reason="content_filter"), TARGET, 1).finish_reason \
        == "content_filter"


def test_a_completed_answer_is_stop_and_a_completed_tool_call_is_tool_calls():
    text = rn.normalize(_resp([{"type": "message", "content": [
        {"type": "output_text", "text": "hi"}]}], USAGE), TARGET, 1)
    assert text.finish_reason == "stop"
    call = rn.normalize(_resp([{"type": "function_call", "call_id": "c", "name": "get_x",
                                "arguments": "{}"}], USAGE), TARGET, 1)
    assert call.finish_reason == "tool_calls"


def test_a_failed_status_passes_through_rather_than_being_dressed_up_as_stop():
    r = rn.normalize(SimpleNamespace(output=[], usage=None, status="failed"), TARGET, 1)
    assert r.finish_reason == "failed"


def test_only_summary_text_parts_become_reasoning_text():
    # The reasoning item carries a `summary` list AND a sibling `content` list of a different
    # part kind. Probed 2026-09-15: every summary part is typed "summary_text".
    r = rn.normalize(_resp([{"type": "reasoning", "summary": [
        {"type": "summary_text", "text": "thought"},
        {"type": "some_future_part", "text": "NOT A SUMMARY"},
    ]}], USAGE), TARGET, 1)
    assert r.reasoning_text == "thought"


def test_output_items_is_what_the_protocol_asks_for_emptiness_with():
    assert rn.output_items(SimpleNamespace(output=None, usage=None, status="failed")) == []
    assert len(rn.output_items(_resp([{"type": "message", "content": []}]))) == 1
