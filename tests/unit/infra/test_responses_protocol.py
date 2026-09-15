# Responsibility: Verify the openai provider resolves to the Responses protocol and calls through it.
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from meshpipeline.adapters.model_inference import call_kwargs as ck
from meshpipeline.adapters.model_inference import protocols, providers
from meshpipeline.adapters.model_inference.protocols import _EmptyResponse
from meshpipeline.contracts.model_routing import RouteTarget

TARGET = RouteTarget(provider="openai", model="gpt-5.6-luna", account="default",
                     circuit_group="openai")


def _patch_client(monkeypatch, client):
    # Patched on providers, NOT on protocols.responses. The protocol must resolve its client
    # through the module at call time: a module-level `client_for = providers.client_for` would
    # survive this patch, and the two safety tests that assert the provider is NEVER dialled
    # (test_failure_handling_wiring.py, test_provider_error_hardening.py) patch exactly here.
    # Under a snapshot they would pass vacuously for openai - and build a real client, i.e. a
    # paid network call from a unit test, on any machine with OPENAI_API_KEY exported.
    monkeypatch.setattr(providers, "client_for", lambda _t: client)


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
    _patch_client(monkeypatch, client)

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
    with pytest.raises(KeyError, match="openai"):
        ck.kwargs_for(TARGET, ck.spec_for("intake"))


def test_the_protocol_resolves_its_client_through_providers_at_call_time(monkeypatch):
    # The patch point the safety suites use. If this protocol ever snapshots client_for at
    # import again, this test is what fails - not a paid call from CI.
    def _refuse(_target):
        raise AssertionError("the protocol dialled the provider")
    monkeypatch.setattr(providers, "client_for", _refuse)

    proto = protocols.protocol_for("openai")
    with pytest.raises(AssertionError, match="dialled"):
        asyncio.run(proto.invoke(
            TARGET, [{"role": "user", "content": "hi"}], tools=None, tool_choice="auto",
            spec=ck.spec_for("intake"), parallel_tool_calls=None, on_reasoning=None,
            label="t", trace={}))


def test_langfuse_kwargs_never_reach_the_unpatched_responses_method(monkeypatch):
    # langfuse 2.36.2 patches Completions/AsyncCompletions/ChatCompletion/Completion and strips
    # {session_id, user_id, name} before the SDK sees them. `responses.create` is NOT patched, so
    # forwarding them raises TypeError -> unclassified -> APPLICATION_DEFECT, which is neither
    # retryable nor failover-eligible: one attempt, terminal, blaming a defect in this product.
    seen = {}

    async def _create(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            output=[{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            usage=None, status="completed")

    _patch_client(monkeypatch, SimpleNamespace(responses=SimpleNamespace(create=_create)))
    asyncio.run(protocols.protocol_for("openai").invoke(
        TARGET, [{"role": "user", "content": "hi"}], tools=None, tool_choice="auto",
        spec=ck.spec_for("intake"), parallel_tool_calls=None, on_reasoning=None, label="t",
        trace={"session_id": "s", "user_id": "u", "name": "intake"}))

    assert not ({"session_id", "user_id", "name"} & set(seen)), (
        f"a langfuse kwarg reached responses.create: {sorted(set(seen))}")


def test_a_response_with_no_output_items_is_refused_rather_than_answered_blank(monkeypatch):
    # The non-streamed twin of chat's `if not (response and response.choices)`. Without it a
    # `failed` (or empty `incomplete`) payload normalises to ok=True with assistant_text="" and
    # a user is handed a blank reply - where the chat path retries and then reports a marker.
    # Intake and the summarizer are the two non-streaming roles, and intake moves first.
    async def _create(**_kwargs):
        return SimpleNamespace(output=[], usage=None, status="failed")

    _patch_client(monkeypatch, SimpleNamespace(responses=SimpleNamespace(create=_create)))
    with pytest.raises(_EmptyResponse, match="no output"):
        asyncio.run(protocols.protocol_for("openai").invoke(
            TARGET, [{"role": "user", "content": "hi"}], tools=None, tool_choice="auto",
            spec=ck.spec_for("intake"), parallel_tool_calls=None, on_reasoning=None,
            label="t", trace={}))


def test_a_truncated_response_is_kept_even_though_it_says_nothing(monkeypatch):
    # The guard above is structural - "did the model return any items" - not "is there text".
    # Probed 2026-09-15 at max_output_tokens=16, a truncation's only item is a `reasoning` one:
    # it carries real usage and maps to finish_reason="length", so the builder's truncation
    # recovery runs. Raising here would burn retries on a request already paid for.
    async def _create(**_kwargs):
        return SimpleNamespace(
            output=[{"type": "reasoning", "summary": []}],
            usage={"input_tokens": 16, "output_tokens": 16,
                   "input_tokens_details": {"cached_tokens": 0},
                   "output_tokens_details": {"reasoning_tokens": 16}},
            status="incomplete", incomplete_details={"reason": "max_output_tokens"})

    _patch_client(monkeypatch, SimpleNamespace(responses=SimpleNamespace(create=_create)))
    proto = protocols.protocol_for("openai")
    response, usage = asyncio.run(proto.invoke(
        TARGET, [{"role": "user", "content": "hi"}], tools=None, tool_choice="auto",
        spec=ck.spec_for("intake"), parallel_tool_calls=None, on_reasoning=None,
        label="t", trace={}))
    assert usage.output_tokens == 16
    assert proto.normalize(response, TARGET, 1).finish_reason == "length"
