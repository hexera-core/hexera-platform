# Responsibility: Verify each role's request matches its declared route, and usage is normalised rather than guessed.
from __future__ import annotations

from types import SimpleNamespace

import pytest

from meshpipeline.adapters._shared.resilience import reset_breakers
from meshpipeline.adapters.model_capacity.local import LocalCapacityController
from meshpipeline.adapters.model_inference import router
from meshpipeline.contracts import inference_telemetry, model_capacity

# THE MATRIX, measured from the pre-routing implementation
# role -> (provider, model, streaming, temperature, max_tokens, extra_body, extra top-level)
# NOTE: temperature 0.3 is a DELIBERATE conservative default for a structured tool-use
# agent. The builder and planner rows LOOK equal here, but that equality is now INTENTIONAL and
# INDEPENDENT: the planner reads its OWN PLANNER_* settings (engines.snappy.settings) and its
# OWN circuit `deepinfra_planner`, so changing a BUILDER_* knob no longer moves the planner. The
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
    record: dict = {}
    resp = _fake_response(True)

    monkeypatch.setattr(router, "client_for", lambda target: _Recorder(record, resp))
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
async def test_the_request_built_for_a_role_matches_its_declared_route(role, probe):
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
    from meshpipeline.adapters.model_inference.router import _usage_from

    u = _usage_from(_fake_response(False))
    assert (u.input_tokens, u.cached_input_tokens, u.output_tokens) == (100, 40, 20)


async def test_direct_deepseek_cache_hit_field_is_understood():
    from meshpipeline.adapters.model_inference.router import _usage_from

    resp = SimpleNamespace(usage=SimpleNamespace(
        prompt_tokens=80, completion_tokens=10, prompt_cache_hit_tokens=64))
    u = _usage_from(resp)
    assert u.cached_input_tokens == 64


async def test_absent_usage_reports_zero_rather_than_guessing():
    from meshpipeline.adapters.model_inference.router import _usage_from

    u = _usage_from(SimpleNamespace())
    assert (u.input_tokens, u.cached_input_tokens, u.output_tokens) == (0, 0, 0)


# the roles reach the providers the matrix says they do
@pytest.mark.parametrize("role", sorted(MATRIX))
async def test_the_route_resolves_to_the_expected_provider_and_model(role):
    from meshpipeline.adapters.model_inference.routes import all_routes

    provider, model = MATRIX[role][0], MATRIX[role][1]
    target = all_routes()[role].primary
    assert (target.provider, target.model) == (provider, model)
    assert all_routes()[role].standby is None, f"{role} gained a standby"


async def test_a_successful_call_returns_no_failure_marker(probe):
    round_result = await router.call_builder_model([{"role": "user", "content": "x"}],
                                                        job_id="j")
    assert round_result.ok and round_result.provider.attempts == 1
