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
