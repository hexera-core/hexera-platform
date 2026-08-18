# Responsibility: Verify every provider round normalises to the agreed surface, exposing no provider object.
from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace

import pytest
from test_provider_error_hardening import _exc

import meshpipeline.adapters.model_inference.router as router
from meshpipeline.adapters._shared.resilience import reset_breakers
from meshpipeline.adapters.model_capacity.local import LocalCapacityController
from meshpipeline.adapters.model_inference.routing import RouteExhausted
from meshpipeline.contracts import model_capacity
from meshpipeline.contracts.model_inference import (
    ModelRoundResult,
    ModelRouter,
    ProviderAttemptInfo,
    ToolCallRequest,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _wired(monkeypatch):
    reset_breakers()
    model_capacity.set_capacity_controller(LocalCapacityController(poll_interval_s=0.001))

    async def _no_sleep(_s, *a, **k):
        return None
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    monkeypatch.setattr("meshpipeline.adapters.model_inference.tracing.langfuse_kwargs",
                        lambda **k: {})
    yield
    model_capacity.set_capacity_controller(None)
    reset_breakers()


def _client(create):
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _tc(cid, name, args):
    return SimpleNamespace(id=cid, function=SimpleNamespace(name=name, arguments=args))


def _provider_response(*, content="", tool_calls=None, finish_reason="stop",
                       prompt=10, completion=2, cached=0, reasoning=None):
    msg = SimpleNamespace(content=content, tool_calls=list(tool_calls) if tool_calls else None)
    if reasoning is not None:
        msg.reasoning_content = reasoning
    usage = SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion,
                            prompt_tokens_details=SimpleNamespace(cached_tokens=cached))
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=finish_reason)],
                           usage=usage, model="served")


def _ok(response):
    async def _create(**kw):
        _create.seen = kw
        return response
    _create.seen = {}
    return _client(_create)


def _no_stream(monkeypatch):
    async def _consume(stream, label="", on_reasoning=None):
        return stream
    monkeypatch.setattr("meshpipeline.adapters.model_inference.streaming.consume_chat_stream",
                        _consume)


# non-streaming (Intake) route
async def test_the_non_streaming_intake_route_normalizes(monkeypatch):
    monkeypatch.setattr(router, "client_for",
                        lambda t: _ok(_provider_response(content="What simulation type?")))
    r = await router.call_intake_model([{"role": "user", "content": "hi"}], job_id="j")
    assert isinstance(r, ModelRoundResult)
    assert r.ok and r.assistant_text == "What simulation type?"
    assert r.finish_reason == "stop" and r.tool_calls == ()
    assert (r.input_tokens, r.output_tokens) == (10, 2)


# streaming (Builder) route
async def test_the_streaming_builder_route_normalizes(monkeypatch):
    _no_stream(monkeypatch)
    monkeypatch.setattr(router, "client_for", lambda t: _ok(_provider_response(
        content="", finish_reason="tool_calls",
        tool_calls=[_tc("c1", "run_mesh", '{"engine": "snappy"}')])))
    r = await router.call_builder_model([{"role": "user", "content": "x"}], tools=[{"a": 1}],
                                        job_id="j")
    assert isinstance(r, ModelRoundResult)
    assert r.finish_reason == "tool_calls"
    assert r.tool_calls == (ToolCallRequest(id="c1", name="run_mesh",
                                            arguments='{"engine": "snappy"}'),)


# streaming multimodal (Reviewer)
async def test_the_streaming_multimodal_reviewer_route_normalizes(monkeypatch):
    _no_stream(monkeypatch)
    seen = {}

    async def _create(**kw):
        seen.update(kw)
        return _provider_response(finish_reason="tool_calls",
                                  tool_calls=[_tc("c9", "inspect_region",
                                                  '{"region_name": "wing"}')])
    monkeypatch.setattr(router, "client_for", lambda t: _client(_create))

    multimodal = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        {"type": "text", "text": "inspect this"}]}]
    r = await router.call_reviewer_with_tools(multimodal, [{"a": 1}], job_id="j")
    assert r.tool_calls[0].name == "inspect_region"
    # the image went OUT on the request; nothing image-shaped comes back on the result
    outbound = json.dumps(seen["messages"], default=str)
    assert "image_url" in outbound
    assert "image" not in json.dumps(r, default=lambda o: o.__dict__)


# several calls per round
async def test_multiple_tool_calls_in_one_round_are_all_normalized(monkeypatch):
    _no_stream(monkeypatch)
    monkeypatch.setattr(router, "client_for", lambda t: _ok(_provider_response(
        finish_reason="tool_calls",
        tool_calls=[_tc("a", "zoom", "{}"), _tc("b", "reset_view", "{}"),
                    _tc("c", "submit_findings", '{"verdict": "PASS"}')])))
    r = await router.call_reviewer_with_tools([{"role": "user", "content": "x"}], [], job_id="j")
    assert [c.name for c in r.tool_calls] == ["zoom", "reset_view", "submit_findings"]
    assert r.tool_calls[2].arguments == '{"verdict": "PASS"}'


async def test_malformed_arguments_survive_verbatim_for_the_caller_to_judge(monkeypatch):
    _no_stream(monkeypatch)
    monkeypatch.setattr(router, "client_for", lambda t: _ok(_provider_response(
        finish_reason="tool_calls", tool_calls=[_tc("x", "zoom", "{not json")])))
    r = await router.call_builder_model([{"role": "user", "content": "x"}], tools=[{"a": 1}],
                                        job_id="j")
    assert r.tool_calls[0].arguments == "{not json"


# plain text and usage
async def test_a_plain_text_turn_reports_no_tool_calls(monkeypatch):
    _no_stream(monkeypatch)
    monkeypatch.setattr(router, "client_for",
                        lambda t: _ok(_provider_response(content="just talking")))
    r = await router.call_builder_model([{"role": "user", "content": "x"}], job_id="j")
    assert r.tool_calls == () and r.assistant_text == "just talking" and r.finish_reason == "stop"


async def test_truncation_is_reported_through_finish_reason(monkeypatch):
    _no_stream(monkeypatch)
    monkeypatch.setattr(router, "client_for",
                        lambda t: _ok(_provider_response(finish_reason="length")))
    r = await router.call_builder_model([{"role": "user", "content": "x"}], job_id="j")
    assert r.finish_reason == "length"


async def test_cached_input_tokens_are_reported_when_the_provider_supplies_them(monkeypatch):
    _no_stream(monkeypatch)
    monkeypatch.setattr(router, "client_for", lambda t: _ok(
        _provider_response(prompt=100, completion=7, cached=64)))
    r = await router.call_builder_model([{"role": "user", "content": "x"}], job_id="j")
    assert (r.input_tokens, r.cached_input_tokens, r.output_tokens) == (100, 64, 7)


# provider attempts: reported, not run
async def test_a_successful_call_reports_one_attempt(monkeypatch):
    _no_stream(monkeypatch)
    monkeypatch.setattr(router, "client_for", lambda t: _ok(_provider_response()))
    r = await router.call_builder_model([{"role": "user", "content": "x"}], job_id="j")
    assert r.provider.attempts == 1
    assert r.provider.provider and r.provider.model


async def test_a_success_after_a_retry_reports_the_real_attempt_count(monkeypatch):
    _no_stream(monkeypatch)
    calls = {"n": 0}

    async def _create(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _exc("connection")
        return _provider_response()
    monkeypatch.setattr(router, "client_for", lambda t: _client(_create))
    r = await router.call_builder_model([{"role": "user", "content": "x"}], job_id="j")
    assert r.ok and calls["n"] == 2 and r.provider.attempts == 2


async def test_terminal_route_exhaustion_becomes_a_marker_not_an_exception(monkeypatch):
    _no_stream(monkeypatch)

    async def _create(**kw):
        raise _exc("rate_limit")
    monkeypatch.setattr(router, "client_for", lambda t: _client(_create))
    r = await router.call_reviewer_with_tools([{"role": "user", "content": "x"}], [], job_id="j")
    assert not r.ok
    assert r.failure_marker == "<<API_FAILURE:reviewer_rate_limit>>"
    assert r.tool_calls == () and r.assistant_text == "" and r.finish_reason == ""
    assert r.provider.attempts >= 1 and r.provider.provider and r.provider.model


# typed request surface
async def test_tool_choice_is_forwarded_verbatim_including_a_forced_function(monkeypatch):
    _no_stream(monkeypatch)
    seen = {}

    async def _create(**kw):
        seen.update(kw)
        return _provider_response()
    monkeypatch.setattr(router, "client_for", lambda t: _client(_create))

    forced = {"type": "function", "function": {"name": "submit_mesh"}}
    await router.call_builder_model([{"role": "user", "content": "x"}], tools=[{"a": 1}],
                                    tool_choice=forced, job_id="j")
    assert seen["tool_choice"] == forced


async def test_parallel_tool_calls_is_omitted_unless_explicitly_set(monkeypatch):
    _no_stream(monkeypatch)
    seen = {}

    async def _create(**kw):
        seen.clear(); seen.update(kw)
        return _provider_response()
    monkeypatch.setattr(router, "client_for", lambda t: _client(_create))

    await router.call_builder_model([{"role": "user", "content": "x"}], tools=[{"a": 1}],
                                    job_id="j")
    assert "parallel_tool_calls" not in seen

    await router.call_builder_model([{"role": "user", "content": "x"}], tools=[{"a": 1}],
                                    job_id="j", parallel_tool_calls=False)
    assert seen["parallel_tool_calls"] is False


async def test_a_toolless_call_sends_neither_tools_nor_tool_choice(monkeypatch):
    _no_stream(monkeypatch)
    seen = {}

    async def _create(**kw):
        seen.update(kw)
        return _provider_response()
    monkeypatch.setattr(router, "client_for", lambda t: _client(_create))
    await router.call_builder_model([{"role": "user", "content": "x"}], job_id="j")
    assert "tools" not in seen and "tool_choice" not in seen


# nothing vendor-shaped escapes
@pytest.mark.parametrize("field", ["tool_calls", "assistant_text", "reasoning_text",
                                   "reasoning_tokens",
                                   "finish_reason", "input_tokens", "cached_input_tokens",
                                   "output_tokens", "provider", "failure_marker"])
def test_the_round_result_surface_is_exactly_the_agreed_fields(field):
    import dataclasses
    names = {f.name for f in dataclasses.fields(ModelRoundResult)}
    assert field in names
    assert names == {"tool_calls", "assistant_text", "reasoning_text", "reasoning_tokens",
                     "finish_reason", "input_tokens", "cached_input_tokens", "output_tokens",
                     "provider", "failure_marker"}


async def test_no_provider_object_is_reachable_from_the_result(monkeypatch):
    _no_stream(monkeypatch)
    monkeypatch.setattr(router, "client_for", lambda t: _ok(_provider_response(
        finish_reason="tool_calls", tool_calls=[_tc("c1", "zoom", "{}")])))
    r = await router.call_builder_model([{"role": "user", "content": "x"}], tools=[{"a": 1}],
                                        job_id="j")
    for obj in (r, r.provider, *r.tool_calls):
        assert type(obj).__module__.startswith("meshpipeline."), type(obj)
        assert not hasattr(obj, "choices") and not hasattr(obj, "model_extra")


async def test_the_summarizer_route_is_normalized_too(monkeypatch):
    monkeypatch.setattr(router, "client_for",
                        lambda t: _ok(_provider_response(content="distilled")))
    r = await router.call_summarizer_model([{"role": "user", "content": "x"}], job_id="j")
    assert isinstance(r, ModelRoundResult) and r.assistant_text == "distilled"


async def test_the_summarizer_stays_respond_or_raise(monkeypatch):
    async def _create(**kw):
        raise _exc("rate_limit")
    monkeypatch.setattr(router, "client_for", lambda t: _client(_create))
    with pytest.raises(RouteExhausted):
        await router.call_summarizer_model([{"role": "user", "content": "x"}], job_id="j")


# the concrete router IS the Protocol
def test_the_adapter_satisfies_the_typed_protocol():
    assert isinstance(router, ModelRouter)


def test_no_router_method_still_advertises_an_untyped_signature():
    for name in ("call_builder_model", "call_planner_model", "call_intake_model",
                 "call_reviewer_with_tools", "call_summarizer_model"):
        sig = inspect.signature(getattr(router, name))
        params = list(sig.parameters.values())
        assert not any(p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD) for p in params), \
            f"{name} still exposes *args/**kwargs"
        assert sig.return_annotation in ("ModelRoundResult", ModelRoundResult), name


def test_the_defaults_are_behaviour_neutral():
    assert ModelRoundResult().ok
    assert ModelRoundResult(failure_marker="x").ok is False
    assert ProviderAttemptInfo() == ProviderAttemptInfo(0, "", "")
