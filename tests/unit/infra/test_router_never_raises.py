# Responsibility: Verify every agent model call answers with a result or a failure marker, never a raw exception.
# Boundaries: the router's per-role entry points, routing admission and the loop's provider round; the
# provider client is a double at the providers.client_for seam.
from __future__ import annotations

import asyncio
import dataclasses
import importlib
from types import SimpleNamespace

import pytest

from meshpipeline.adapters._shared.resilience import reset_breakers
from meshpipeline.adapters.model_capacity.local import LocalCapacityController
from meshpipeline.adapters.model_inference import providers, router
from meshpipeline.adapters.model_inference import routes as routecfg
from meshpipeline.contracts import inference_telemetry, model_capacity
from meshpipeline.contracts.event_stream import StaleExecutionPublish
from meshpipeline.contracts.execution_guard import StaleWorkerFenced
from meshpipeline.errors import FailureClass, classify_api_failure

_ROUTE_HOMES = (
    ("meshpipeline.agents.builder.settings", "BUILDER_ROUTE"),
    ("meshpipeline.engines.snappy.settings", "PLANNER_ROUTE"),
    ("meshpipeline.agents.reviewer.settings", "VISUAL_REVIEWER_ROUTE"),
    ("meshpipeline.agents.intake.settings", "INTAKE_ROUTE"),
)
MSG = [{"role": "user", "content": "x"}]


@pytest.fixture(autouse=True)
def _wired(monkeypatch):
    reset_breakers()
    # chat-shaped doubles below, so every role is pinned to a chat provider (as in
    # test_provider_error_hardening.pin_chat_routes)
    for mod_name, attr in _ROUTE_HOMES:
        module = importlib.import_module(mod_name)
        route = getattr(module, attr)
        monkeypatch.setattr(module, attr, dataclasses.replace(
            route, primary=dataclasses.replace(route.primary, provider="deepinfra")))
    monkeypatch.setattr(routecfg, "_ROUTES", None)
    model_capacity.set_capacity_controller(LocalCapacityController(poll_interval_s=0.001))

    class _Sink:
        def record(self, call): pass
    inference_telemetry.set_inference_telemetry(_Sink())

    async def _no_sleep(_s, *a, **k):
        return None
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    monkeypatch.setattr("meshpipeline.adapters.model_inference.tracing.langfuse_kwargs",
                        lambda **k: {})

    async def _consume(stream, label="", on_reasoning=None):
        return stream
    monkeypatch.setattr("meshpipeline.adapters.model_inference.streaming.consume_chat_stream",
                        _consume)
    yield
    inference_telemetry.set_inference_telemetry(None)
    model_capacity.set_capacity_controller(None)
    reset_breakers()
    routecfg._ROUTES = None


def _ok_client():
    msg = SimpleNamespace(content="ok", tool_calls=None)
    resp = SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")],
                           usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2),
                           model="served")

    async def _create(**kwargs):
        return resp
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))


def _raising_client(exc):
    async def _create(**kwargs):
        raise exc
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))


async def _call(role: str):
    if role == "builder":
        return await router.call_builder_model(MSG, job_id="j")
    if role == "planner":
        return await router.call_planner_model(MSG, job_id="j")
    return await router.call_reviewer_with_tools(MSG, [], job_id="j")


ROLES = ["builder", "planner", "reviewer"]
PREFIX = {"builder": "builder", "planner": "builder", "reviewer": "reviewer"}


class _BrokenController:
    def __init__(self, exc):
        self.exc = exc

    async def acquire(self, *_a, **_k):
        raise self.exc

    async def release(self, *_a, **_k):
        return None

    async def depth(self, *_a, **_k):
        return 0


# capacity admission
@pytest.mark.parametrize("role", ROLES)
async def test_a_capacity_error_is_a_retryable_marker(role, monkeypatch):
    monkeypatch.setattr(providers, "client_for", lambda t: _ok_client())
    model_capacity.set_capacity_controller(
        _BrokenController(model_capacity.CapacityError("controller store unreachable")))
    out = await _call(role)
    assert out.failure_marker == f"<<API_FAILURE:{PREFIX[role]}_transient>>"
    assert classify_api_failure(out.failure_marker) is FailureClass.PROVIDER_TRANSIENT


@pytest.mark.parametrize("role", ROLES)
async def test_no_capacity_controller_at_all_is_a_marker(role, monkeypatch):
    monkeypatch.setattr(providers, "client_for", lambda t: _ok_client())
    model_capacity.set_capacity_controller(None)
    out = await _call(role)
    assert out.failure_marker and classify_api_failure(out.failure_marker).is_retryable


@pytest.mark.parametrize("role", ROLES)
async def test_a_local_controller_crash_is_a_marker(role, monkeypatch):
    monkeypatch.setattr(providers, "client_for", lambda t: _ok_client())
    model_capacity.set_capacity_controller(_BrokenController(OSError("redis went away")))
    out = await _call(role)
    assert out.failure_marker == f"<<API_FAILURE:{PREFIX[role]}_transient>>"


async def test_a_release_that_breaks_never_costs_the_answer(monkeypatch):
    monkeypatch.setattr(providers, "client_for", lambda t: _ok_client())

    class _BadRelease(LocalCapacityController):
        async def release(self, lease):
            raise RuntimeError("release broke")
    model_capacity.set_capacity_controller(_BadRelease(poll_interval_s=0.001))
    out = await router.call_builder_model(MSG, job_id="j")
    assert not out.failure_marker and out.assistant_text == "ok"


# normalize
@pytest.mark.parametrize("role", ROLES)
async def test_an_exception_reading_the_answer_is_a_marker(role, monkeypatch):
    monkeypatch.setattr(providers, "client_for", lambda t: _ok_client())
    real = router.protocol_for

    def _protocol(provider):
        p = real(provider)

        def _normalize(*_a, **_k):
            raise KeyError("choices")
        return SimpleNamespace(invoke=p.invoke, normalize=_normalize)
    monkeypatch.setattr(router, "protocol_for", _protocol)
    out = await _call(role)
    assert out.failure_marker == f"<<API_FAILURE:{PREFIX[role]}_empty_response>>"
    assert classify_api_failure(out.failure_marker).is_retryable


# a raw provider exception
@pytest.mark.parametrize("role", ROLES)
async def test_a_raw_provider_exception_is_a_marker(role, monkeypatch):
    monkeypatch.setattr(providers, "client_for", lambda t: _raising_client(ValueError("boom")))
    out = await _call(role)
    assert out.failure_marker == f"<<API_FAILURE:{PREFIX[role]}_non_transient>>"


@pytest.mark.parametrize("role", ROLES)
async def test_anything_escaping_the_route_is_a_marker(role, monkeypatch):
    async def _boom(*_a, **_k):
        raise RuntimeError("routing bug")
    monkeypatch.setattr(router, "execute", _boom)
    out = await _call(role)
    assert out.failure_marker.startswith(f"<<API_FAILURE:{PREFIX[role]}_")


# the fences and cancellation still end the call
@pytest.mark.parametrize("exc", [StaleExecutionPublish("stale"),
                                 StaleWorkerFenced("w", "j", 1, "t"),
                                 asyncio.CancelledError()])
async def test_fences_and_cancellation_are_never_swallowed(exc, monkeypatch):
    async def _raise(*_a, **_k):
        raise exc
    monkeypatch.setattr(router, "execute", _raise)
    with pytest.raises(type(exc)):
        await router.call_builder_model(MSG, job_id="j")


@pytest.mark.parametrize("exc", [StaleExecutionPublish("stale"),
                                 StaleWorkerFenced("w", "j", 1, "t")])
async def test_a_fence_during_admission_is_never_swallowed(exc, monkeypatch):
    monkeypatch.setattr(providers, "client_for", lambda t: _ok_client())
    model_capacity.set_capacity_controller(_BrokenController(exc))
    with pytest.raises(type(exc)):
        await router.call_builder_model(MSG, job_id="j")


# the loop's provider round, behind the router
def _round(call):
    from meshpipeline.agents.loop.provider_round import ProviderRound
    return ProviderRound(provider_call=call, tools=[], job_id="j")


def _acct():
    from meshpipeline.agents.loop.accounting import AgentRunAccountant
    from meshpipeline.contracts.agent_loop import AgentRole, LoopLimits
    return AgentRunAccountant(role=AgentRole.builder, job_id="j", limits=LoopLimits())


async def test_a_provider_call_that_raises_ends_the_round_as_a_provider_failure():
    from meshpipeline.agents.loop.provider_round import ROUND_RAISED_MARKER, RoundEnded
    from meshpipeline.contracts.agent_loop import LoopExit

    async def _call(**_kw):
        raise RuntimeError("a stand-in router that raises")
    out = await _round(_call).invoke(MSG, acct=_acct(), forced_tool=None, remaining=None,
                                     round_no=1)
    assert isinstance(out, RoundEnded) and out.exit is LoopExit.provider_failed
    assert out.failure_marker == ROUND_RAISED_MARKER
    assert classify_api_failure(out.failure_marker) is FailureClass.PROVIDER_TRANSIENT


@pytest.mark.parametrize("exc", [StaleExecutionPublish("stale"),
                                 StaleWorkerFenced("w", "j", 1, "t")])
async def test_the_provider_round_lets_the_fences_through(exc):
    async def _call(**_kw):
        raise exc
    with pytest.raises(type(exc)):
        await _round(_call).invoke(MSG, acct=_acct(), forced_tool=None, remaining=None,
                                   round_no=1)
