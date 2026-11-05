# Responsibility: Verify a conversation survives the provider seam intact - tool calls, results, images and reasoning.
from __future__ import annotations

import pytest

from meshpipeline.adapters.model_inference.messages import to_provider_messages


def test_a_plain_exchange_serialises_unchanged():
    convo = [
        {"role": "system", "content": "you are a mesher"},
        {"role": "user", "content": "mesh this"},
        {"role": "assistant", "content": "on it"},
    ]
    assert to_provider_messages(convo) == convo


def test_tool_calls_and_tool_results_survive_intact():
    convo = [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "run_mesh", "arguments": '{"engine":"cfmesh"}'}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "rc=0"},
    ]
    out = to_provider_messages(convo)
    assert out[0]["tool_calls"][0]["id"] == "call_1"
    assert out[0]["tool_calls"][0]["function"]["name"] == "run_mesh"
    assert out[0]["content"] is None, "an assistant turn that only calls tools keeps content=None"
    assert out[1] == {"role": "tool", "tool_call_id": "call_1", "content": "rc=0"}


def test_a_multimodal_review_turn_survives_intact():
    convo = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        {"type": "text", "text": "is this mesh sound?"},
    ]}]
    out = to_provider_messages(convo)
    assert out[0]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert out[0]["content"][1] == {"type": "text", "text": "is this mesh sound?"}


def test_assistant_reasoning_is_preserved_on_the_wire():
    convo = [{"role": "assistant", "content": "thinking out loud",
              "reasoning_content": "step 1 ... step 2 ..."}]
    out = to_provider_messages(convo)
    assert out[0]["reasoning_content"] == "step 1 ... step 2 ...", (
        "the model's prior reasoning was dropped from the request")


def test_the_conversation_is_copied_not_aliased():
    convo = [{"role": "user", "content": "hi"}]
    out = to_provider_messages(convo)
    out[0]["content"] = "MUTATED"
    assert convo[0]["content"] == "hi", "the adapter can corrupt the agent's conversation"


def test_a_turn_without_a_role_is_not_silently_sent():
    with pytest.raises((ValueError, KeyError)):
        to_provider_messages([{"content": "no role"}])   # type: ignore[list-item]


def test_the_router_serialises_through_the_seam():
    import inspect

    from meshpipeline.adapters.model_inference import router
    src = inspect.getsource(router)
    creates = src.count("chat.completions.create(")
    converted = src.count("to_provider_messages(messages)")
    assert converted == creates, (
        f"{creates} SDK call(s) but only {converted} go through the conversion seam")


def test_the_neutral_vocabulary_is_product_owned():
    import inspect

    from meshpipeline.contracts import model_inference
    src = inspect.getsource(model_inference)
    assert "Conversation" in src and "Message" in src and "Role" in src
    # the port itself imports no SDK - the docstring may NAME the vendor it replaced
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert "openai" not in stripped, f"the port imports a vendor SDK: {stripped!r}"
