# Responsibility: Verify each provider exception normalises to its category, and an unknown one is our defect.
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from meshpipeline.adapters._shared.resilience import get_breaker, reset_breakers
from meshpipeline.adapters.model_capacity.local import LocalCapacityController
from meshpipeline.adapters.model_inference import providers, router
from meshpipeline.contracts import inference_telemetry, model_capacity
from meshpipeline.contracts.model_routing import FailureCategory as FC


def _openai():
    import openai
    return openai


def _exc(kind: str):
    o = _openai()
    req = SimpleNamespace(method="POST", url="/chat/completions")
    resp = SimpleNamespace(status_code=429, headers={}, request=req)
    if kind == "rate_limit":
        return o.RateLimitError("429 too many", response=resp, body=None)
    if kind == "timeout":
        return o.APITimeoutError(request=req)
    if kind == "connection":
        return o.APIConnectionError(request=req)
    if kind == "server":
        return o.InternalServerError("500 boom", response=resp, body=None)
    if kind == "auth":
        return o.AuthenticationError("401", response=resp, body=None)
    if kind == "bad_request":
        return o.BadRequestError("400 bad", response=resp, body=None)
    raise AssertionError(kind)


@pytest.fixture(autouse=True)
def _wired(monkeypatch):
    reset_breakers()
    model_capacity.set_capacity_controller(LocalCapacityController(poll_interval_s=0.001))
    records: list = []

    class _Sink:
        def record(self, call): records.append(call)
    inference_telemetry.set_inference_telemetry(_Sink())

    async def _no_sleep(_s, *a, **k):     # the schedule is proven in test_route_timing_equivalence
        return None
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    monkeypatch.setattr("meshpipeline.adapters.model_inference.tracing.langfuse_kwargs",
                        lambda **k: {})
    yield records
    inference_telemetry.set_inference_telemetry(None)
    model_capacity.set_capacity_controller(None)
    reset_breakers()


def _raising_client(exc):
    async def _create(**kwargs):
        raise exc
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))


def _ok_client(response):
    async def _create(**kwargs):
        return response
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))


def _response():
    msg = SimpleNamespace(content="ok", tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")],
                           usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2),
                           model="served")


def _no_stream(monkeypatch):
    async def _consume(stream, label="", on_reasoning=None):
        return stream
    monkeypatch.setattr("meshpipeline.adapters.model_inference.streaming.consume_chat_stream",
                        _consume)


# provider exception -> normalized category (was TestClassifyErrorReason)
@pytest.mark.parametrize("kind,category", [
    ("rate_limit", FC.RATE_LIMIT),
    ("timeout",    FC.TIMEOUT),
    ("connection", FC.CONNECTION),
    ("server",     FC.SERVICE_UNAVAILABLE),
    ("auth",       FC.AUTH),
    ("bad_request", FC.INVALID_REQUEST),
])
def test_a_provider_exception_normalises_to_its_category(kind, category):
    assert providers.classify(_exc(kind)) is category


def test_an_unknown_exception_is_an_application_defect_not_provider_weather():
    assert providers.classify(ValueError("something we did")) is FC.APPLICATION_DEFECT


def test_a_balance_error_is_not_a_transient_provider_fault():
    o = _openai()
    resp = SimpleNamespace(status_code=400, headers={},
                           request=SimpleNamespace(method="POST", url="/x"))
    exc = o.BadRequestError("Insufficient balance on your account", response=resp, body=None)
    assert providers.classify(exc) is FC.INSUFFICIENT_BALANCE


# end-to-end: exception -> the EXACT established marker (was TestCall*FailureReason)
BUILDER_CASES = [
    ("rate_limit",  "<<API_FAILURE:builder_rate_limit>>"),
    ("timeout",     "<<API_FAILURE:builder_timeout>>"),
    ("connection",  "<<API_FAILURE:builder_connection>>"),
    ("server",      "<<API_FAILURE:builder_server_error>>"),
    ("auth",        "<<API_FAILURE:builder_non_transient>>"),
    ("bad_request", "<<API_FAILURE:builder_non_transient>>"),
]


@pytest.mark.parametrize("kind,marker", BUILDER_CASES, ids=[k for k, _ in BUILDER_CASES])
async def test_the_builder_returns_its_established_marker_for_each_provider_failure(
        kind, marker, monkeypatch):
    _no_stream(monkeypatch)
    monkeypatch.setattr(router, "client_for", lambda t: _raising_client(_exc(kind)))
    round_result = await router.call_builder_model([{"role": "user", "content": "x"}],
                                                        job_id="j")
    assert not round_result.ok
    assert round_result.failure_marker == marker


@pytest.mark.parametrize("kind,marker", [
    ("rate_limit", "<<API_FAILURE:reviewer_rate_limit>>"),
    ("timeout",    "<<API_FAILURE:reviewer_timeout>>"),
    ("server",     "<<API_FAILURE:reviewer_server_error>>"),
], ids=["rate_limit", "timeout", "server"])
async def test_the_visual_reviewer_returns_its_established_marker(kind, marker, monkeypatch):
    _no_stream(monkeypatch)
    monkeypatch.setattr(router, "client_for", lambda t: _raising_client(_exc(kind)))
    round_result = await router.call_reviewer_with_tools([{"role": "user", "content": "x"}], [],
                                                        job_id="j")
    assert round_result.failure_marker == marker


async def test_the_planner_emits_builder_markers_as_it_always_has(monkeypatch):
    _no_stream(monkeypatch)
    monkeypatch.setattr(router, "client_for", lambda t: _raising_client(_exc("server")))
    round_result = await router.call_planner_model([{"role": "user", "content": "x"}], job_id="j")
    assert round_result.failure_marker == "<<API_FAILURE:builder_server_error>>"


@pytest.mark.parametrize("kind", ["rate_limit", "timeout", "connection", "server", "auth"])
async def test_intake_collapses_every_reason_to_unavailable_as_it_always_has(kind, monkeypatch):
    monkeypatch.setattr(router, "client_for", lambda t: _raising_client(_exc(kind)))
    round_result = await router.call_intake_model([{"role": "user", "content": "x"}], job_id="j")
    assert round_result.failure_marker == "<<API_FAILURE:intake_unavailable>>"


# empty response + circuit open
async def test_an_empty_response_is_retried_then_reported_as_empty_response(monkeypatch):
    _no_stream(monkeypatch)
    empty = SimpleNamespace(choices=[], usage=None)
    calls = 0

    def _client(_t):
        async def _create(**kw):
            nonlocal calls
            calls += 1
            return empty
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))

    monkeypatch.setattr(router, "client_for", _client)
    round_result = await router.call_builder_model([{"role": "user", "content": "x"}], job_id="j")
    assert round_result.failure_marker == "<<API_FAILURE:builder_empty_response>>"
    assert calls == 3, "an empty response must be retried against the same model"


async def test_an_open_circuit_fails_fast_without_dialling_the_provider(monkeypatch):
    dialled = 0

    def _client(_t):
        nonlocal dialled
        dialled += 1
        return _ok_client(_response())

    monkeypatch.setattr(router, "client_for", _client)
    b = get_breaker("deepinfra_builder")
    for _ in range(b.failure_threshold):
        b.record_failure()

    round_result = await router.call_builder_model([{"role": "user", "content": "x"}], job_id="j")
    assert round_result.failure_marker == "<<API_FAILURE:builder_circuit_open>>"
    assert dialled == 0, "an open circuit still dialled the provider"


async def test_a_terminal_failure_is_never_retried(monkeypatch):
    _no_stream(monkeypatch)
    calls = 0

    def _client(_t):
        async def _create(**kw):
            nonlocal calls
            calls += 1
            raise _exc("auth")
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))

    monkeypatch.setattr(router, "client_for", _client)
    round_result = await router.call_builder_model([{"role": "user", "content": "x"}], job_id="j")
    assert round_result.failure_marker == "<<API_FAILURE:builder_non_transient>>"
    assert calls == 1, f"a terminal auth failure was retried {calls} times"


async def test_a_retryable_failure_then_success_returns_the_response(monkeypatch):
    _no_stream(monkeypatch)
    calls = 0

    def _client(_t):
        async def _create(**kw):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise _exc("connection")
            return _response()
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))

    monkeypatch.setattr(router, "client_for", _client)
    round_result = await router.call_builder_model([{"role": "user", "content": "x"}],
                                                        job_id="j")
    # the router RETRIED once and reports it - the accounting a loop consumes without retrying
    assert round_result.ok and round_result.provider.attempts == 2 and calls == 2
    assert round_result.assistant_text == "ok"


async def test_a_successful_call_is_never_executed_twice(monkeypatch):
    _no_stream(monkeypatch)
    calls = 0

    def _client(_t):
        async def _create(**kw):
            nonlocal calls
            calls += 1
            return _response()
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))

    monkeypatch.setattr(router, "client_for", _client)
    await router.call_builder_model([{"role": "user", "content": "x"}], job_id="j")
    assert calls == 1


# telemetry reports what actually happened
async def test_telemetry_reports_the_actual_provider_and_model_used(_wired, monkeypatch):
    _no_stream(monkeypatch)
    monkeypatch.setattr(router, "client_for", lambda t: _ok_client(_response()))
    await router.call_builder_model([{"role": "user", "content": "x"}], job_id="job-9")
    rec = _wired[-1]
    assert rec.role == "builder"
    assert rec.provider == "deepinfra" and rec.model == "zai-org/GLM-5.2"
    assert rec.target == "primary" and rec.failure_category == ""
    assert rec.job_id == "job-9"


async def test_telemetry_records_the_failure_category_for_a_failed_call(_wired, monkeypatch):
    _no_stream(monkeypatch)
    monkeypatch.setattr(router, "client_for", lambda t: _raising_client(_exc("rate_limit")))
    await router.call_builder_model([{"role": "user", "content": "x"}], job_id="j")
    assert _wired[-1].failure_category == FC.RATE_LIMIT.value


async def test_a_telemetry_failure_never_changes_the_call_result(monkeypatch):
    class _Broken:
        def record(self, call):
            raise RuntimeError("sink down")
    inference_telemetry.set_inference_telemetry(_Broken())
    _no_stream(monkeypatch)
    monkeypatch.setattr(router, "client_for", lambda t: _ok_client(_response()))
    round_result = await router.call_builder_model([{"role": "user", "content": "x"}],
                                                        job_id="j")
    assert round_result.ok and round_result.assistant_text == "ok"


# capacity leases
@pytest.mark.parametrize("kind", ["rate_limit", "auth"])
async def test_the_lease_is_released_after_a_failed_role_call(kind, monkeypatch):
    from meshpipeline.adapters.model_inference.routes import all_routes
    _no_stream(monkeypatch)
    monkeypatch.setattr(router, "client_for", lambda t: _raising_client(_exc(kind)))
    await router.call_builder_model([{"role": "user", "content": "x"}], job_id="j")
    domain = all_routes()["builder"].primary.domain.key
    assert await model_capacity.depth(domain) == 0


async def test_builder_and_planner_contend_for_one_quota_domain(monkeypatch):
    from meshpipeline.adapters.model_inference.routes import all_routes
    assert (all_routes()["builder"].primary.domain.key
            == all_routes()["planner"].primary.domain.key)


# summarizer: respond or raise
async def test_the_summarizer_raises_and_never_returns_none(monkeypatch):
    from meshpipeline.adapters.model_inference.routing import RouteExhausted

    original = _exc("connection")
    monkeypatch.setattr(router, "client_for", lambda t: _raising_client(original))
    with pytest.raises(RouteExhausted) as ei:
        await router.call_summarizer_model([{"role": "user", "content": "x"}], job_id="j")
    assert ei.value.category is FC.CONNECTION
    assert ei.value.provider == "deepseek" and ei.value.model == "deepseek-v4-flash"
    assert isinstance(ei.value.__cause__, type(original))


async def test_the_summarizer_returns_the_response_on_success(monkeypatch):
    monkeypatch.setattr(router, "client_for", lambda t: _ok_client(_response()))
    round_result = await router.call_summarizer_model([{"role": "user", "content": "x"}],
                                                     job_id="j")
    assert round_result.ok and round_result.assistant_text == "ok"
