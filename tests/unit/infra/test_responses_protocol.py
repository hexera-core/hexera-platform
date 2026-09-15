# Responsibility: Verify the openai provider resolves to the Responses protocol and calls through it.
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from meshpipeline.adapters.model_inference import call_kwargs as ck, protocols
from meshpipeline.contracts.model_routing import RouteTarget

TARGET = RouteTarget(provider="openai", model="gpt-5.6-luna", account="default",
                     circuit_group="openai")


def test_openai_resolves_to_the_responses_protocol():
    assert protocols.protocol_for("openai").__class__.__name__ == "Responses"


def test_the_protocol_calls_responses_create_not_chat_completions(monkeypatch):
    seen = {}

    async def _create(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            output=[{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            usage={"input_tokens": 1, "output_tokens": 1,
                   "input_tokens_details": {"cached_tokens": 0},
                   "output_tokens_details": {"reasoning_tokens": 0}},
            status="completed")

    client = SimpleNamespace(responses=SimpleNamespace(create=_create))
    monkeypatch.setattr(
        "meshpipeline.adapters.model_inference.protocols.responses.client_for",
        lambda _t: client)

    proto = protocols.protocol_for("openai")
    response, usage = asyncio.run(proto.invoke(
        TARGET, [{"role": "user", "content": "hi"}], tools=None, tool_choice="auto",
        spec=ck.spec_for("intake"), parallel_tool_calls=None, on_reasoning=None,
        label="t", trace={}))

    assert "max_output_tokens" in seen and "max_tokens" not in seen
    assert usage.input_tokens == 1
    assert proto.normalize(response, TARGET, 1).assistant_text == "ok"


def test_call_kwargs_no_longer_claims_to_serve_openai():
    # The parameter-only arm is superseded: a Responses request is not a kwargs dict.
    import pytest
    with pytest.raises(KeyError, match="openai"):
        ck.kwargs_for(TARGET, ck.spec_for("intake"))
