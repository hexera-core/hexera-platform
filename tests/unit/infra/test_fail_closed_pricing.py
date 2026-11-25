# Responsibility: Verify an unpriced model is REFUSED rather than metered at $0.00, and that the repair_v4_propose
# call kind reaches the wire with its images, tools, cache key and job id.
# Boundaries: the real pricing table, router and Responses request builder; the provider client is a double at the
# providers.client_for seam, so nothing here can reach a network.
from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

import pytest

from meshpipeline.adapters._shared.resilience import reset_breakers
from meshpipeline.adapters.inference_telemetry import pricing
from meshpipeline.adapters.model_capacity.local import LocalCapacityController
from meshpipeline.adapters.model_inference import providers, router
from meshpipeline.adapters.model_inference import routes as routecfg
from meshpipeline.adapters.model_inference.protocols.responses_request import build_request
from meshpipeline.adapters.model_inference.routing import Usage, execute
from meshpipeline.contracts import inference_telemetry, model_capacity
from meshpipeline.contracts import model_inference as mi
from meshpipeline.contracts.model_routing import ModelRoute, RouteTarget


@pytest.fixture(autouse=True)
def _wired():
    reset_breakers()
    model_capacity.set_capacity_controller(LocalCapacityController(poll_interval_s=0.001))
    seen: list = []

    class _Sink:
        def record(self, call):
            seen.append(call)
    inference_telemetry.set_inference_telemetry(_Sink())
    yield seen
    inference_telemetry.set_inference_telemetry(None)
    model_capacity.set_capacity_controller(None)
    reset_breakers()


# --- the primitive -------------------------------------------------------------------------------------------


def test_require_price_returns_the_declared_price():
    assert pricing.require_price("openai", "gpt-6-astra") == (10.0, 50.0, 1.0)


def test_require_price_refuses_an_unpriced_model_and_names_the_remedy():
    with pytest.raises(mi.UnpricedModel) as err:
        pricing.require_price("openai", "gpt-9-mystery")
    assert "openai:gpt-9-mystery" in str(err.value) and "MODEL_PRICE_OVERRIDES" in str(err.value)


def test_a_declared_override_makes_a_model_priced(monkeypatch):
    import meshpipeline.settings.inference_overrides as io
    monkeypatch.setattr(io, "price_overrides", lambda: {"openai:gpt-9-mystery": (1.0, 2.0, 0.5)})
    assert pricing.require_price("openai", "gpt-9-mystery") == (1.0, 2.0, 0.5)


def test_a_declared_free_model_is_priced_not_unpriced(monkeypatch):
    import meshpipeline.settings.inference_overrides as io
    monkeypatch.setattr(io, "price_overrides", lambda: {"openai:free-one": (0.0, 0.0, 0.0)})
    assert pricing.require_price("openai", "free-one") == (0.0, 0.0, 0.0)


def test_the_lenient_lookup_is_unchanged():
    # telemetry still degrades to 0.0 for a model it cannot price; only the DIAL is closed
    assert pricing.price_for("openai", "gpt-9-mystery") == (0.0, 0.0, 0.0)


# --- per call, before the provider is dialled ------------------------------------------------------------------


def test_execute_refuses_an_unpriced_target_without_invoking_it():
    target = RouteTarget(provider="openai", model="gpt-9-mystery", circuit_group="g")
    route = ModelRoute(role="t", primary=target, require_price=True)
    invoked = []

    async def invoke(t):
        invoked.append(t)
        return None, None

    with pytest.raises(mi.UnpricedModel, match="MODEL_PRICE_OVERRIDES"):
        asyncio.run(execute(route, invoke, lambda e: None, job_id="j"))
    assert invoked == []


def test_a_route_that_did_not_ask_keeps_the_lenient_behaviour():
    # existing roles carry no cap that depends on the meter; startup (below) is where they are held to it
    target = RouteTarget(provider="openai", model="gpt-9-mystery", circuit_group="g")
    route = ModelRoute(role="t", primary=target)
    invoked = []

    async def invoke(t):
        invoked.append(t)
        return "answer", Usage()

    result, _target, _attempts = asyncio.run(execute(route, invoke, lambda e: None, job_id="j"))
    assert result == "answer" and invoked == [target]


# --- the startup seam --------------------------------------------------------------------------------------------


def test_nothing_is_unpriced_in_the_shipped_configuration():
    assert routecfg.unpriced_role_models() == [] and routecfg.unpriced_proposer_models() == []


def test_a_legacy_role_override_is_reported_as_a_role_model_not_a_proposer_one(monkeypatch):
    monkeypatch.setenv("BUILDER_MODEL", "gpt-9-mystery")
    assert "openai:gpt-9-mystery" in routecfg.unpriced_role_models()
    assert routecfg.unpriced_proposer_models() == []


def test_the_proposer_chain_is_checked_separately(monkeypatch):
    monkeypatch.setattr(mi, "REPAIR_V4_PROPOSE_ROUTES", ("openai:gpt-9-mystery", "openai:gpt-6-astra"))
    assert routecfg.unpriced_proposer_models() == ["openai:gpt-9-mystery"]


def _warnings_of(fn):
    import warnings
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        fn()
    return [str(w.message) for w in caught]


def test_hardened_startup_refuses_only_an_unpriced_proposer_model(monkeypatch):
    from meshpipeline.runtime import startup
    from meshpipeline.settings.env import ConfigurationError
    monkeypatch.setattr(routecfg, "unpriced_role_models", lambda: [])
    monkeypatch.setattr(routecfg, "unpriced_proposer_models", lambda: ["openai:gpt-9-mystery"])
    with pytest.raises(ConfigurationError, match="MODEL_PRICE_OVERRIDES"):
        startup.check_models_priced(hardened=True)
    assert any("MODEL_PRICE_OVERRIDES" in m for m in _warnings_of(lambda: startup.check_models_priced(hardened=False)))


def test_hardened_startup_never_refuses_because_of_a_legacy_role(monkeypatch):
    # production posture is untouched: a legacy role with an unpriced override warns and boots
    from meshpipeline.runtime import startup
    monkeypatch.setattr(routecfg, "unpriced_role_models", lambda: ["openai:gpt-9-legacy"])
    monkeypatch.setattr(routecfg, "unpriced_proposer_models", lambda: [])
    messages = _warnings_of(lambda: startup.check_models_priced(hardened=True))     # does not raise
    assert any("gpt-9-legacy" in m for m in messages)


# --- the repair_v4_propose call kind -------------------------------------------------------------------------------


def _client(captured: dict):
    async def _create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            output=[{"type": "function_call", "call_id": "c1", "name": "propose",
                     "arguments": '{"candidates": []}'}],
            usage={"input_tokens": 1000, "output_tokens": 100,
                   "input_tokens_details": {"cached_tokens": 600},
                   "output_tokens_details": {"reasoning_tokens": 0}},
            status="completed")
    return SimpleNamespace(responses=SimpleNamespace(create=_create))


PNG_URL = "data:image/png;base64," + base64.b64encode(b"png").decode()
TOOLS = [mi.ToolSchema("propose", "d", {"type": "object", "properties": {}}).definition()]
MESSAGES = [{"role": "system", "content": "SYS"},
            {"role": "user", "content": [{"type": "text", "text": "look"},
                                         {"type": "image_url", "image_url": {"url": PNG_URL}}]}]


def test_the_call_carries_images_tools_cache_key_and_prices_the_usage(monkeypatch, _wired):
    captured: dict = {}
    monkeypatch.setattr(providers, "client_for", lambda t: _client(captured))
    router_obj = router
    mi.set_model_router(router_obj)
    out = asyncio.run(mi.call_repair_v4_propose(MESSAGES, TOOLS, route="openai:gpt-6-astra", job_id="job-42",
                                                cache_key="k1"))
    assert captured["model"] == "gpt-6-astra"
    assert captured["prompt_cache_key"] == "k1"
    assert captured["instructions"] == "SYS"
    assert captured["tools"][0]["name"] == "propose"
    assert captured["input"][0]["content"][1] == {"type": "input_image", "image_url": PNG_URL}
    assert [c.name for c in out.reply.tool_calls] == ["propose"]
    # 400 fresh x $10 + 600 cached x $1 + 100 out x $50, per million
    assert out.cost_usd == pytest.approx((400 * 10 + 600 * 1 + 100 * 50) / 1e6)
    assert (out.provider, out.model) == ("openai", "gpt-6-astra")
    [rec] = _wired
    assert rec.job_id == "job-42" and rec.role == "repair_v4_propose" and rec.model == "gpt-6-astra"


def test_the_call_refuses_an_unpriced_route_before_dialling(monkeypatch):
    dialled = []
    monkeypatch.setattr(providers, "client_for", lambda t: dialled.append(t))
    mi.set_model_router(router)
    with pytest.raises(mi.UnpricedModel, match="MODEL_PRICE_OVERRIDES"):
        asyncio.run(mi.call_repair_v4_propose(MESSAGES, TOOLS, route="openai:gpt-9-mystery"))
    assert dialled == []


def test_a_failed_provider_answer_is_a_reply_with_a_marker_and_no_cost(monkeypatch):
    async def _create(**kw):
        raise ValueError("boom")        # not retryable, not ours to classify further
    monkeypatch.setattr(providers, "client_for",
                        lambda t: SimpleNamespace(responses=SimpleNamespace(create=_create)))
    mi.set_model_router(router)
    out = asyncio.run(mi.call_repair_v4_propose(MESSAGES, TOOLS, route="openai:gpt-6-astra"))
    assert out.reply.failure_marker and out.cost_usd == 0.0


def test_a_router_without_the_call_kind_is_refused_by_name():
    class Old:
        pass
    mi.set_model_router(Old())          # type: ignore[arg-type]
    try:
        with pytest.raises(mi.ToolsUnsupported):
            asyncio.run(mi.call_repair_v4_propose(MESSAGES, TOOLS, route="openai:gpt-6-astra"))
    finally:
        mi.set_model_router(router)


def test_build_request_sends_the_cache_key_only_when_asked():
    from meshpipeline.adapters.model_inference.call_kwargs import SamplingSpec
    t = RouteTarget(provider="openai", model="gpt-6-astra", circuit_group="g")
    msgs = [{"role": "user", "content": "x"}]
    plain = build_request(t, msgs, tools=None, tool_choice="auto", spec=SamplingSpec(max_tokens=10),
                          parallel_tool_calls=None)
    keyed = build_request(t, msgs, tools=None, tool_choice="auto",
                          spec=SamplingSpec(max_tokens=10, cache_key="abc"), parallel_tool_calls=None)
    assert "prompt_cache_key" not in plain and keyed["prompt_cache_key"] == "abc"


def test_the_installed_caching_router_forwards_the_call_kind_without_recording_it():
    # composition installs CachingModelRouter, not the bare router; without a forwarder the call kind
    # would raise ToolsUnsupported in production while every test against the bare router passed
    from meshpipeline.contracts.decision_cache import CachingModelRouter
    seen = {}

    class Inner:
        async def call_repair_v4_propose(self, **kw):
            seen.update(kw)
            return "forwarded"

    wrapped = CachingModelRouter(Inner(), model_of=lambda step: "m", store=object(), mode="record")
    mi.set_model_router(wrapped)       # type: ignore[arg-type]
    try:
        out = asyncio.run(mi.call_repair_v4_propose(MESSAGES, TOOLS, route="openai:gpt-6-astra",
                                                    job_id="j", user_id="u"))
    finally:
        mi.set_model_router(router)
    assert out == "forwarded" and seen["job_id"] == "j" and seen["route"] == "openai:gpt-6-astra"


# --- cache-write pricing -------------------------------------------------------------------------------------


def test_cache_write_tokens_cost_their_own_rate_not_the_input_rate():
    base = {"input_tokens": 1_000_000, "output_tokens": 0}
    plain = pricing.estimate_cost("openai", "gpt-6-astra", **base)
    w30 = pricing.estimate_cost("openai", "gpt-6-astra", **base, cache_write_tokens=1_000_000,
                                cache_write_30m_tokens=1_000_000)
    w12 = pricing.estimate_cost("openai", "gpt-6-astra", **base, cache_write_tokens=1_000_000)
    assert (plain, w30, w12) == (10.0, 12.5, 25.0)


def test_a_model_with_unconfirmed_write_rates_is_flagged_and_never_given_an_invented_one(caplog):
    assert pricing.cache_write_unpriced() == ["openai:gpt-5.6-luna", "openai:gpt-5.6-sol", "openai:gpt-5.6-terra"]
    pricing._warned.discard("cw:openai:gpt-5.6-terra")
    with caplog.at_level("WARNING"):
        cost = pricing.estimate_cost("openai", "gpt-5.6-terra", input_tokens=1_000_000, output_tokens=0,
                                     cache_write_tokens=1_000_000, cache_write_30m_tokens=1_000_000)
    assert cost == 2.0 and "no confirmed cache-write price" in caplog.text


def test_cache_write_tokens_are_not_billed_twice():
    # the write tokens are part of input_tokens: fresh input is what is left after cached AND written
    c = pricing.estimate_cost("openai", "gpt-6-astra", input_tokens=100, output_tokens=0,
                              cached_input_tokens=40, cache_write_tokens=50, cache_write_30m_tokens=50)
    assert c == pytest.approx((10 * 10.0 + 40 * 1.0 + 50 * 12.5) / 1e6)
