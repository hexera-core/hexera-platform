# tests/unit/infra/test_responses_stream.py
# Responsibility: Verify a Responses event stream assembles into the same shape the sync call returns.
# Boundaries: assembly only; no network.
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from meshpipeline.adapters.model_inference.protocols import _EmptyResponse
from meshpipeline.adapters.model_inference.protocols import responses_normalize as rn
from meshpipeline.adapters.model_inference.protocols.responses_stream import (
    consume_responses_stream,
)
from meshpipeline.contracts.model_routing import RouteTarget

TARGET = RouteTarget(provider="openai", model="gpt-5.6-luna", account="default",
                     circuit_group="openai")


async def _iter(events):
    for e in events:
        yield e


def _ev(t, **kw):
    return SimpleNamespace(type=t, **kw)


def test_the_terminal_completed_event_wins_over_disagreeing_deltas():
    # Deltas deliberately DISAGREE with the completed event's text ("Hello" vs "Hello, world") so
    # an implementation that reconstructed assistant_text from deltas - rather than trusting
    # response.completed as the sole source of output[] - would fail this, pinning the design
    # decision instead of merely being consistent with it.
    events = [
        _ev("response.created"),
        _ev("response.output_text.delta", delta="Hel"),
        _ev("response.output_text.delta", delta="lo"),
        _ev("response.output_text.done"),
        _ev("response.completed", response=SimpleNamespace(
            output=[{"type": "message",
                     "content": [{"type": "output_text", "text": "Hello, world"}]}],
            usage={"input_tokens": 4, "output_tokens": 2,
                   "input_tokens_details": {"cached_tokens": 0},
                   "output_tokens_details": {"reasoning_tokens": 0}},
            status="completed")),
    ]
    assembled = asyncio.run(consume_responses_stream(_iter(events), label="t", on_reasoning=None))
    result = rn.normalize(assembled, TARGET, 1)
    assert result.assistant_text == "Hello, world"
    assert (result.input_tokens, result.output_tokens) == (4, 2)


def test_the_terminal_completed_event_wins_over_disagreeing_tool_call_deltas():
    # Same pin as above, for tool-call arguments: function_call_arguments.delta fragments are
    # ignored (this module keeps no accumulator for them) and the completed event's arguments
    # string is what reaches the caller, deliberately disagreeing with the deltas' concatenation.
    events = [
        _ev("response.output_item.added",
            item={"type": "function_call", "call_id": "call_2", "name": "get_x"}),
        _ev("response.function_call_arguments.delta", delta='{"a"'),
        _ev("response.function_call_arguments.delta", delta=':"1"}'),
        _ev("response.function_call_arguments.done", arguments='{"a":"1"}'),
        _ev("response.completed", response=SimpleNamespace(
            output=[{"type": "function_call", "call_id": "call_2", "name": "get_x",
                     "arguments": '{"a":"2"}'}],
            usage=None, status="completed")),
    ]
    assembled = asyncio.run(consume_responses_stream(_iter(events), label="t", on_reasoning=None))
    result = rn.normalize(assembled, TARGET, 1)
    assert [(c.name, c.arguments) for c in result.tool_calls] == [("get_x", '{"a":"2"}')]


def test_reasoning_summary_deltas_reach_the_sink_cumulatively_as_they_arrive():
    # ReasoningSink's contract (contracts/model_inference.py:55-56) is "the reasoning so far", the
    # same convention consume_chat_stream uses for chat - so each call carries the running total,
    # not just the newest fragment. Pinned as an exact sequence, mirroring
    # test_live_reasoning_stream.py's ["a", "ab"].
    seen = []
    events = [
        _ev("response.reasoning_summary_text.delta", delta="think"),
        _ev("response.reasoning_summary_text.delta", delta="ing"),
        _ev("response.completed", response=SimpleNamespace(
            output=[], usage=None, status="completed")),
    ]
    asyncio.run(consume_responses_stream(_iter(events), label="t", on_reasoning=seen.append))
    assert seen == ["think", "thinking"]


def test_a_stream_with_no_terminal_event_at_all_does_not_invent_a_result():
    # A DEAD stream - no terminal event of any kind, so no output and no usage - must not look
    # like a successful empty answer. The caller's _EmptyResponse path exists for exactly this
    # and classifies to EMPTY_RESPONSE, retryable against the same target.
    with pytest.raises(_EmptyResponse):
        asyncio.run(consume_responses_stream(_iter([_ev("response.created")]),
                                             label="t", on_reasoning=None))


def test_a_truncated_stream_returns_its_partial_answer_instead_of_being_thrown_away():
    # Probed 2026-09-15 with max_output_tokens=16: a truncated stream NEVER emits
    # response.completed - it terminates on response.incomplete, whose .response carries the
    # partial output[], status="incomplete" and the real usage. Acting only on
    # response.completed discarded a request the account had already paid for and burned two
    # more retries on an output cap that no retry can lift.
    events = [
        _ev("response.created"),
        _ev("response.output_text.delta", delta="Turbulence mod"),
        _ev("response.incomplete", response=SimpleNamespace(
            output=[{"type": "message",
                     "content": [{"type": "output_text", "text": "Turbulence mod"}]}],
            usage={"input_tokens": 16, "output_tokens": 16,
                   "input_tokens_details": {"cached_tokens": 0},
                   "output_tokens_details": {"reasoning_tokens": 16}},
            status="incomplete", incomplete_details={"reason": "max_output_tokens"})),
    ]
    assembled = asyncio.run(consume_responses_stream(_iter(events), label="t", on_reasoning=None))
    result = rn.normalize(assembled, TARGET, 1)
    assert result.assistant_text == "Turbulence mod"
    assert result.output_tokens == 16, "the round is billed whether or not it finished"
    # The whole point of returning it: agents/builder/loop.py:91 reads this word.
    assert result.finish_reason == "length"


def test_a_failed_stream_is_raised_rather_than_normalised_as_a_successful_answer():
    # response.failed reports a SERVER-SIDE ERROR, not a short answer. Returning it would
    # normalise to ok=True carrying whatever fragment preceded the failure - a broken reply
    # presented as a complete one.
    events = [
        _ev("response.created"),
        _ev("response.failed", response=SimpleNamespace(
            output=[{"type": "message",
                     "content": [{"type": "output_text", "text": "half an ans"}]}],
            usage=None, status="failed",
            error={"code": "server_error", "message": "boom"})),
    ]
    with pytest.raises(_EmptyResponse, match="response.failed"):
        asyncio.run(consume_responses_stream(_iter(events), label="t", on_reasoning=None))
