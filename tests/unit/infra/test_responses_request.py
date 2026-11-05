# tests/unit/infra/test_responses_request.py
# Responsibility: Verify a neutral Conversation becomes the exact request the Responses API accepts.
# Boundaries: shape only; no network and no SDK.
from __future__ import annotations

import pytest

from meshpipeline.adapters.model_inference import call_kwargs as ck
from meshpipeline.adapters.model_inference.protocols import responses_request as rr
from meshpipeline.contracts.model_routing import RouteTarget

TARGET = RouteTarget(provider="openai", model="gpt-5.6-luna", account="default",
                     circuit_group="openai")


def test_a_system_turn_becomes_top_level_instructions():
    instructions, items = rr.to_input_items([
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hi"},
    ])
    assert instructions == "be brief"
    assert items == [{"role": "user", "content": "hi"}]


def test_several_system_turns_are_joined_not_dropped():
    instructions, items = rr.to_input_items([
        {"role": "system", "content": "one"},
        {"role": "system", "content": "two"},
        {"role": "user", "content": "hi"},
    ])
    assert "one" in instructions and "two" in instructions
    assert items == [{"role": "user", "content": "hi"}]


def test_an_assistant_tool_call_becomes_a_function_call_item():
    _instr, items = rr.to_input_items([
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": "get_x", "arguments": '{"a":"1"}'}}]},
    ])
    assert items[-1] == {"type": "function_call", "call_id": "call_1",
                         "name": "get_x", "arguments": '{"a":"1"}'}


def test_an_assistant_turn_with_both_text_and_tool_calls_orders_text_then_calls():
    # Pins the shape agents/loop/provider_round.py:120-125's assistant_turn() actually emits -
    # every tool-calling round for builder, planner and reviewer builds EXACTLY this dict
    # unconditionally: {"role": "assistant", "content": <text>, "tool_calls": [...]}, never
    # content=None. Probed 2026-09-15 against /v1/responses: a bare {"role":"assistant",
    # "content":...} item followed by sibling function_call / function_call_output items in one
    # `input` array was ACCEPTED (status: completed) and the model read the tool output through
    # it correctly ("The result is **42**."). So the text item must come first and must survive.
    _instr, items = rr.to_input_items([
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "Let me check that.",
         "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": "get_x", "arguments": '{"a":"1"}'}}]},
    ])
    assert items[-2:] == [
        {"role": "assistant", "content": "Let me check that."},
        {"type": "function_call", "call_id": "call_1", "name": "get_x",
         "arguments": '{"a":"1"}'},
    ]


def test_a_tool_result_becomes_a_function_call_output_item():
    _instr, items = rr.to_input_items([
        {"role": "tool", "tool_call_id": "call_1", "name": "get_x", "content": "42"},
    ])
    assert items == [{"type": "function_call_output", "call_id": "call_1", "output": "42"}]


def test_a_multimodal_turn_uses_input_text_and_a_FLAT_input_image():
    # Probed 2026-09-15: the chat shape {"type":"image_url","image_url":{"url":...}} is refused
    # by name - "Supported values are: 'input_text', 'input_image'".
    _instr, items = rr.to_input_items([
        {"role": "user", "content": [
            {"type": "text", "text": "colour?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        ]},
    ])
    assert items[0]["content"] == [
        {"type": "input_text", "text": "colour?"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAA"},
    ]


def test_tool_definitions_are_flattened_from_the_chat_shape():
    out = rr.to_responses_tools([
        {"type": "function", "function": {"name": "get_x", "description": "d",
                                          "parameters": {"type": "object", "properties": {}}}}
    ])
    assert out == [{"type": "function", "name": "get_x", "description": "d",
                    "parameters": {"type": "object", "properties": {}}}]


def test_the_request_uses_max_output_tokens_and_no_rejected_sampling():
    req = rr.build_request(TARGET, [{"role": "user", "content": "hi"}],
                           tools=None, tool_choice="auto",
                           spec=ck.spec_for("intake"), parallel_tool_calls=None)
    assert req["model"] == "gpt-5.6-luna"
    assert req["max_output_tokens"] == ck.spec_for("intake").max_tokens
    for rejected in ("max_tokens", "temperature", "top_p", "presence_penalty",
                     "min_p", "top_k", "extra_body"):
        assert rejected not in req, f"{rejected} is a 400 on this API"


def test_a_forced_tool_choice_is_flattened_like_the_tool_declaration():
    # Probed 2026-09-15: chat's {"type":"function","function":{"name":...}} - which
    # agents/loop/provider_round.py:62-64 builds for every forced round - is a 400 here,
    # "Missing required parameter: 'tool_choice.name'". The flat shape is accepted.
    req = rr.build_request(
        TARGET, [{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "get_x", "parameters": {}}}],
        tool_choice={"type": "function", "function": {"name": "get_x"}},
        spec=ck.spec_for("intake"), parallel_tool_calls=None)
    assert req["tool_choice"] == {"type": "function", "name": "get_x"}


@pytest.mark.parametrize("choice", ["auto", "required", "none"])
def test_the_string_tool_choices_are_identical_on_both_protocols(choice):
    req = rr.build_request(
        TARGET, [{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "get_x", "parameters": {}}}],
        tool_choice=choice, spec=ck.spec_for("intake"), parallel_tool_calls=None)
    assert req["tool_choice"] == choice


def test_a_reasoning_role_asks_for_the_summary_that_the_sink_consumes():
    # Probed 2026-09-15: without `reasoning.summary` the reasoning item's summary is [] and zero
    # reasoning_summary_text.delta events arrive - so responses_stream's sink and normalize's
    # _summary_text_from_reasoning would never fire. The builder does not set thinking_off.
    req = rr.build_request(TARGET, [{"role": "user", "content": "hi"}],
                           tools=None, tool_choice="auto",
                           spec=ck.spec_for("builder"), parallel_tool_calls=None)
    assert req["reasoning"] == {"summary": "auto"}


def test_a_thinking_off_role_does_not_ask_to_have_reasoning_narrated_back():
    # intake and the summarizer declare they want no chain-of-thought. Paying for a summary of
    # reasoning they asked not to have is incoherent; reasoning_text stays "" for them (§11).
    for role in ("intake", "summarizer"):
        req = rr.build_request(TARGET, [{"role": "user", "content": "hi"}],
                               tools=None, tool_choice="auto",
                               spec=ck.spec_for(role), parallel_tool_calls=None)
        assert "reasoning" not in req, role
