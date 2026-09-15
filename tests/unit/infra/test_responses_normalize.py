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
