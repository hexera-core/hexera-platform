# Responsibility: Verify a refused model call keeps the provider's own words, with credentials masked.
# Boundaries: routing.describe_failure and the RouteExhausted it feeds; classification and the
# stored marker are pinned elsewhere (test_provider_error_hardening, test_model_routing).
from __future__ import annotations

from types import SimpleNamespace

import pytest

from meshpipeline.adapters._shared.resilience import reset_breakers
from meshpipeline.adapters.model_capacity.local import LocalCapacityController
from meshpipeline.adapters.model_inference import providers
from meshpipeline.adapters.model_inference.routing import (
    RouteExhausted,
    describe_failure,
    execute,
)
from meshpipeline.contracts import inference_telemetry, model_capacity
from meshpipeline.contracts.model_routing import (
    Capability,
    FailureCategory,
    ModelRoute,
    RetryPolicy,
    RouteTarget,
)

#: DeepInfra's answer to every call once the account ran dry - job 4f18812f's review, 2026-09-30.
_BALANCE = "You need positive balance to do inference. Please add balance manually or setup top-up"


def _status_error(code: int, message: str):
    import openai
    resp = SimpleNamespace(status_code=code, headers={},
                           request=SimpleNamespace(method="POST", url="/chat/completions"))
    return openai.APIStatusError(message, response=resp, body=None)


def test_an_empty_account_is_told_in_the_providers_words_not_as_a_type_name():
    detail = describe_failure(_status_error(402, _BALANCE))
    assert detail.startswith("APIStatusError 402: ")
    assert "positive balance" in detail


def test_a_credential_in_the_providers_message_is_masked():
    detail = describe_failure(_status_error(
        401, "Incorrect API key provided: sk-proj-AbCdEf0123456789XyZ. Bearer abcdefgh12345678"))
    assert "401" in detail and "Incorrect API key" in detail
    assert "AbCdEf0123456789" not in detail and "abcdefgh12345678" not in detail


def test_a_long_message_is_trimmed():
    detail = describe_failure(_status_error(400, "word " * 400))
    assert len(detail) <= 240 + len("APIStatusError 400: ")
    assert detail.endswith("...")


def test_an_exception_with_nothing_to_say_is_its_type_name():
    assert describe_failure(RuntimeError()) == "RuntimeError"


@pytest.fixture
def _routing():
    reset_breakers()
    model_capacity.set_capacity_controller(LocalCapacityController(poll_interval_s=0.001))

    class _Sink:
        def record(self, call): pass

    inference_telemetry.set_inference_telemetry(_Sink())
    yield
    inference_telemetry.set_inference_telemetry(None)
    model_capacity.set_capacity_controller(None)
    reset_breakers()


async def test_the_exhausted_route_carries_the_refusal_to_the_log(_routing):
    route = ModelRoute(
        role="visual_reviewer",
        primary=RouteTarget(provider="deepinfra", model="Qwen/Qwen3-VL-235B-A22B-Thinking",
                            circuit_group="grp_detail"),
        standby=None, required_capabilities=frozenset({Capability.TOOLS}), timeout_s=0.5,
        retry=RetryPolicy(max_attempts=3, backoff_base_s=0, backoff_max_s=0, jitter=0),
        concurrency_budget=4, queue_deadline_s=0.2)
    calls: list[int] = []

    async def _invoke(_t):
        calls.append(1)
        raise _status_error(402, _BALANCE)

    with pytest.raises(RouteExhausted) as ei:
        await execute(route, _invoke, providers.classify, job_id="4f18812f")
    assert ei.value.category is FailureCategory.INSUFFICIENT_BALANCE
    assert calls == [1], "an empty account is terminal - retrying it only spends the budget"
    assert "402" in ei.value.detail and "positive balance" in ei.value.detail
