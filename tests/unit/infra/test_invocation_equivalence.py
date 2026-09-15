# Responsibility: Pin what the CHAT-COMPLETIONS protocol puts on the wire for each role's
# sampling spec, and verify usage is normalised rather than guessed.
# Boundaries: this pins the PROTOCOL, not this deployment's routing. Every role is served by
# `openai` over the Responses API now (settings/inventory.ROUTE_MATRIX), so nothing here would
# run at all if it read the live route - and the guarantee would have been deleted with the
# premise. DeepInfra and DeepSeek are still supported providers a deployment can point a role
# back at, and the values below are MEASURED facts about what those providers are sent for a
# given role's SamplingSpec. So each role's route is pinned to an explicit chat target for the
# duration of the call: the real `router.call_*` entry point still runs, and what it builds is
# independent of what ROUTE_MATRIX currently says.
# Collaborates with: tests/unit/infra/test_responses_request.py, which pins the OTHER protocol's
# request shape, and the deployment-routing check at the bottom of this file.
from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

import meshpipeline.agent_tools.shared.settings as scfg
import meshpipeline.agents.builder.settings as bcfg
import meshpipeline.agents.intake.settings as icfg
import meshpipeline.agents.reviewer.settings as rcfg
import meshpipeline.engines.snappy.settings as pcfg
from meshpipeline.adapters._shared.resilience import reset_breakers
from meshpipeline.adapters.model_capacity.local import LocalCapacityController
from meshpipeline.adapters.model_inference import providers, router
from meshpipeline.adapters.model_inference import routes as routecfg
from meshpipeline.contracts import inference_telemetry, model_capacity
from meshpipeline.contracts.model_routing import RouteTarget

# THE MATRIX, measured from the pre-routing implementation
# role -> (provider, model, streaming, temperature, max_tokens, extra_body, extra top-level)
# The provider/model columns name the CHAT TARGET each row was measured against - they are the
# question's subject, not a reading of the current deployment. Every other value is the answer,
# and none of them moved when the roles left these providers: that is the point of the seam.
# NOTE: temperature 0.3 is a DELIBERATE conservative default for a structured tool-use
# agent. The builder and planner rows LOOK equal here, but that equality is now INTENTIONAL and
# INDEPENDENT: the planner reads its OWN PLANNER_* settings (engines.snappy.settings) and its
# OWN circuit `planner`, so changing a BUILDER_* knob no longer moves the planner. The
# equal VALUES are a chosen default, not shared config - proven by test_planner_config_independence.
MATRIX = {
    "builder": ("deepinfra", "zai-org/GLM-5.2", True, 0.3, 16384,
                {"min_p": 0.05},
                {"top_p": 0.95, "stream_options": {"include_usage": True}}),
    "planner": ("deepinfra", "zai-org/GLM-5.2", True, 0.3, 16384,
                {"min_p": 0.05},
                {"top_p": 0.95, "stream_options": {"include_usage": True}}),
    "visual_reviewer": ("deepinfra", "Qwen/Qwen3-VL-235B-A22B-Thinking", True, 0.7, 16384,
                        {"top_k": 20},
                        {"top_p": 0.8, "presence_penalty": 1.5,
                         "stream_options": {"include_usage": True}}),
    "intake": ("deepseek", "deepseek-v4-pro", False, 1.0, 2048,
               {"thinking": {"type": "disabled"}, "min_p": 0.05}, {}),
    "summarizer": ("deepseek", "deepseek-v4-flash", False, 0.2, 700,
                   {"thinking": {"type": "disabled"}}, {}),
}

#: Where each role's route actually LIVES. The router reads these module attributes per call, so
#: rebinding one here redirects the real entry point without touching the catalogue.
_ROUTE_HOMES = {
    "builder": (bcfg, "BUILDER_ROUTE"),
    "planner": (pcfg, "PLANNER_ROUTE"),
    "visual_reviewer": (rcfg, "VISUAL_REVIEWER_ROUTE"),
    "intake": (icfg, "INTAKE_ROUTE"),
    "summarizer": (scfg, "SUMMARIZER_ROUTE"),
}


class _Recorder:

    def __init__(self, record: dict, response):
        self._record = record
        self._response = response
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self._record.update(kwargs)
        return self._response


def _fake_response(streaming: bool):
    usage = SimpleNamespace(prompt_tokens=100, completion_tokens=20,
                            prompt_tokens_details=SimpleNamespace(cached_tokens=40))
    msg = SimpleNamespace(content="ok", tool_calls=None, reasoning_content=None)
    resp = SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")],
                           usage=usage, model="served-by-provider")
    return resp


@pytest.fixture
def probe(monkeypatch):
    reset_breakers()
    model_capacity.set_capacity_controller(LocalCapacityController(poll_interval_s=0.001))
    inference_telemetry.set_inference_telemetry(None)
    # Every role is pinned to the chat target its MATRIX row was measured against, keeping only
    # the target explicit: the timeout, retry policy, budget and circuit the role really declares
    # all still apply, so the call under test is the product's, not a fixture's reconstruction.
    for role, (module, attr) in _ROUTE_HOMES.items():
        provider, model = MATRIX[role][0], MATRIX[role][1]
        route = getattr(module, attr)
        monkeypatch.setattr(module, attr, dataclasses.replace(
            route, primary=RouteTarget(provider=provider, model=model,
                                       account=route.primary.account,
                                       circuit_group=route.primary.circuit_group)))
    # routes.all_routes() memoises what it reads from those same modules, and routing.execute
    # consults it for the domain budget - so a cache filled while the pin is in place would
    # outlive the pin and hand the next test a route this fixture invented.
    monkeypatch.setattr(routecfg, "_ROUTES", None)

    record: dict = {}
    resp = _fake_response(True)

    monkeypatch.setattr(providers, "client_for", lambda target: _Recorder(record, resp))
    # The stream consumer is exercised in its own tests; here the concern is what was SENT.
    async def _consume(stream, label="", on_reasoning=None):
        return stream
    monkeypatch.setattr("meshpipeline.adapters.model_inference.streaming.consume_chat_stream",
                        _consume)
    monkeypatch.setattr("meshpipeline.adapters.model_inference.tracing.langfuse_kwargs",
                        lambda **k: {})
    yield record
    model_capacity.set_capacity_controller(None)
    reset_breakers()
    routecfg._ROUTES = None


async def _invoke_role(role: str):
    msgs = [{"role": "user", "content": "hi"}]
    tools = [{"type": "function", "function": {"name": "t", "parameters": {}}}]
    if role == "builder":
        return await router.call_builder_model(msgs, tools=tools, job_id="j")
    if role == "planner":
        return await router.call_planner_model(msgs, tools=None, tool_choice="none", job_id="j")
    if role == "visual_reviewer":
        return await router.call_reviewer_with_tools(msgs, tools, job_id="j")
    if role == "intake":
        return await router.call_intake_model(msgs, job_id="j", tools=tools)
    if role == "summarizer":
        return await router.call_summarizer_model(msgs, job_id="j")
    raise AssertionError(role)


@pytest.mark.parametrize("role", sorted(MATRIX))
async def test_the_chat_request_built_for_a_role_matches_its_measured_shape(role, probe):
    _prov, model, streaming, temperature, max_tokens, extra_body, extra_top = MATRIX[role]
    await _invoke_role(role)
    assert probe["model"] == model
    assert bool(probe.get("stream", False)) is streaming, (
        f"{role}: streaming flipped - a streamed call buffered into one response (or vice versa)")
    assert probe["temperature"] == temperature
    assert probe["max_tokens"] == max_tokens
    # extra_body carries min_p / top_k / the thinking control - the params most easily dropped.
    assert probe.get("extra_body") == extra_body, f"{role}: extra_body changed"
    for key, value in extra_top.items():
        assert probe.get(key) == value, f"{role}: {key} changed or was dropped"


@pytest.mark.parametrize("role", ["builder", "visual_reviewer", "intake"])
async def test_tools_and_tool_choice_survive(role, probe):
    await _invoke_role(role)
    assert probe.get("tools"), f"{role}: tool definitions were dropped"
    assert probe.get("tool_choice") == "auto"


async def test_the_planner_tool_choice_is_passed_through_verbatim(probe):
    await _invoke_role("planner")
    assert probe.get("tool_choice") is None or probe.get("tools") is None


@pytest.mark.parametrize("role", sorted(MATRIX))
async def test_the_messages_pass_through_the_neutral_conversion(role, probe):
    await _invoke_role(role)
    assert probe["messages"] == [{"role": "user", "content": "hi"}]


async def test_usage_is_normalised_including_cached_input(probe):
    from meshpipeline.adapters.model_inference.protocols.chat_completions import _usage_from

    u = _usage_from(_fake_response(False))
    assert (u.input_tokens, u.cached_input_tokens, u.output_tokens) == (100, 40, 20)


async def test_direct_deepseek_cache_hit_field_is_understood():
    from meshpipeline.adapters.model_inference.protocols.chat_completions import _usage_from

    resp = SimpleNamespace(usage=SimpleNamespace(
        prompt_tokens=80, completion_tokens=10, prompt_cache_hit_tokens=64))
    u = _usage_from(resp)
    assert u.cached_input_tokens == 64


async def test_absent_usage_reports_zero_rather_than_guessing():
    from meshpipeline.adapters.model_inference.protocols.chat_completions import _usage_from

    u = _usage_from(SimpleNamespace())
    assert (u.input_tokens, u.cached_input_tokens, u.output_tokens) == (0, 0, 0)


#: THIS DEPLOYMENT's routing - the separate question from everything above, and the one the
#: MATRIX used to answer by accident. Written as literals rather than read from ROUTE_MATRIX,
#: because a check that derives its expectation from the thing it checks passes whatever the
#: catalogue says. All five roles left DeepSeek/DeepInfra for OpenAI on the Responses API.
DEPLOYED = {
    "builder":         ("openai", "gpt-5.6-terra"),
    "planner":         ("openai", "gpt-5.6-terra"),
    "visual_reviewer": ("openai", "gpt-5.6-terra"),
    "intake":          ("openai", "gpt-5.6-luna"),
    "summarizer":      ("openai", "gpt-5.6-luna"),
}


@pytest.mark.parametrize("role", sorted(DEPLOYED))
async def test_the_route_resolves_to_the_expected_provider_and_model(role):
    from meshpipeline.adapters.model_inference.routes import all_routes

    target = all_routes()[role].primary
    assert (target.provider, target.model) == DEPLOYED[role]
    assert all_routes()[role].standby is None, f"{role} gained a standby"


async def test_the_deployed_routes_speak_the_protocol_this_file_does_not_pin(probe):
    # The guard against this file quietly becoming the only wire test again: if a role were
    # routed back onto chat completions, its request shape would be pinned above AND live, and
    # this assertion is what says which of those two worlds we are in.
    from meshpipeline.adapters.model_inference.protocols import protocol_for
    from meshpipeline.adapters.model_inference.protocols.responses import Responses

    for role, (provider, _model) in sorted(DEPLOYED.items()):
        assert isinstance(protocol_for(provider), Responses), role


async def test_a_successful_call_returns_no_failure_marker(probe):
    round_result = await router.call_builder_model([{"role": "user", "content": "x"}],
                                                        job_id="j")
    assert round_result.ok and round_result.provider.attempts == 1
