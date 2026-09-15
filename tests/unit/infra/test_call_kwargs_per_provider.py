# Responsibility: Verify each provider receives only parameters it accepts.
from __future__ import annotations

import pytest

from meshpipeline.adapters.model_inference import call_kwargs as ck
from meshpipeline.contracts.model_routing import RouteTarget


def _t(provider: str, model: str = "m") -> RouteTarget:
    return RouteTarget(provider=provider, model=model, account="default",
                       circuit_group=provider)


@pytest.mark.parametrize("role", ["builder", "planner", "visual_reviewer", "intake",
                                  "summarizer"])
def test_openai_never_receives_a_vendor_sampling_extension(role):
    """min_p and top_k are vLLM/SGLang sampler fields. OpenAI rejects an unknown parameter
    outright, so a route pointed at openai would 400 on every call."""
    kw = ck.kwargs_for(_t("openai"), ck.spec_for(role))
    body = kw.get("extra_body", {})
    assert "min_p" not in body and "top_k" not in body, f"{role}: {body}"
    assert "min_p" not in kw and "top_k" not in kw


@pytest.mark.parametrize("role", ["intake", "summarizer"])
def test_openai_never_receives_deepseeks_thinking_switch(role):
    kw = ck.kwargs_for(_t("openai"), ck.spec_for(role))
    assert "thinking" not in kw.get("extra_body", {})


def test_openai_still_carries_the_roles_real_intent():
    kw = ck.kwargs_for(_t("openai", "gpt-5.6-terra"), ck.spec_for("builder"))
    assert kw["model"] == "gpt-5.6-terra"
    assert kw["temperature"] == pytest.approx(0.3)
    assert kw["top_p"] == pytest.approx(0.95)
    assert kw["stream"] is True
    assert kw["stream_options"] == {"include_usage": True}


def test_deepinfra_still_gets_its_extensions():
    kw = ck.kwargs_for(_t("deepinfra"), ck.spec_for("builder"))
    assert kw["extra_body"]["min_p"] == pytest.approx(0.05)


def test_an_unregistered_provider_is_refused_not_guessed():
    with pytest.raises(KeyError, match="anthropic"):
        ck.kwargs_for(_t("anthropic"), ck.spec_for("builder"))
