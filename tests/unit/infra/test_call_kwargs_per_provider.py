# Responsibility: Verify each provider receives only parameters it accepts.
from __future__ import annotations

import pytest

from meshpipeline.adapters.model_inference import call_kwargs as ck
from meshpipeline.adapters.model_inference.protocols.responses_request import build_request
from meshpipeline.contracts.model_routing import RouteTarget

# A trivial conversation is enough - build_request's translation of `input` items is exercised
# by tests/unit/infra/test_responses_request.py; the concern here is only which SAMPLING fields
# survive into the request for a given role, which is orthogonal to what the conversation says.
_CONVO = [{"role": "user", "content": "hi"}]


def _t(provider: str, model: str = "m") -> RouteTarget:
    return RouteTarget(provider=provider, model=model, account="default",
                       circuit_group=provider)


def _openai_request_for(role: str, model: str = "m") -> dict:
    # OpenAI's request is no longer a kwargs dict from call_kwargs (see
    # test_call_kwargs_no_longer_claims_to_serve_openai in test_responses_protocol.py) - it is
    # built by responses_request.build_request, exactly as protocols/responses.py calls it per
    # attempt. These tests moved to call it directly so they keep pinning what they always
    # pinned: which of a role's real SamplingSpec fields survive translation for openai.
    return build_request(_t("openai", model), _CONVO, tools=None, tool_choice="auto",
                         spec=ck.spec_for(role), parallel_tool_calls=None)


@pytest.mark.parametrize("role", ["builder", "planner", "visual_reviewer", "intake",
                                  "summarizer"])
def test_openai_never_receives_a_vendor_sampling_extension(role):
    """min_p and top_k are vLLM/SGLang sampler fields. The Responses API rejects an unknown
    parameter outright (400), so a route pointed at openai would fail every call that carried
    them - checked over the whole request, since a Responses body has no extra_body escape
    hatch for a provider extension to hide in."""
    request = _openai_request_for(role)
    assert "min_p" not in request and "top_k" not in request, f"{role}: {request}"


@pytest.mark.parametrize("role", ["intake", "summarizer"])
def test_openai_never_receives_deepseeks_thinking_switch(role):
    assert "thinking" not in _openai_request_for(role)


def test_openai_still_carries_the_roles_real_intent():
    request = _openai_request_for("builder", model="gpt-5.6-terra")
    assert request["model"] == "gpt-5.6-terra"
    assert request["max_output_tokens"] == ck.spec_for("builder").max_tokens
    assert request["stream"] is True
    # The builder's SamplingSpec carries temperature, top_p and min_p - all real tuning - but
    # every one of them is a 400 on the Responses API (design doc §1), so none may leak into
    # the request just because build_request otherwise preserves the role's intent.
    for field in ("temperature", "top_p", "presence_penalty", "min_p", "top_k"):
        assert field not in request, f"{field} leaked into the openai request: {request}"


def test_deepinfra_still_gets_its_extensions():
    kw = ck.kwargs_for(_t("deepinfra"), ck.spec_for("builder"))
    assert kw["extra_body"]["min_p"] == pytest.approx(0.05)


def test_an_unregistered_provider_is_refused_not_guessed():
    with pytest.raises(KeyError, match="anthropic"):
        ck.kwargs_for(_t("anthropic"), ck.spec_for("builder"))
