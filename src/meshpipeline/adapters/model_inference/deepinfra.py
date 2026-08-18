# Responsibility: Build the DeepInfra client the builder and reviewer call through.
# Boundaries: client construction and credentials; no routing, no retries.
from __future__ import annotations

import meshpipeline.agents.builder.settings as bcfg
import meshpipeline.agents.reviewer.settings as rcfg
import meshpipeline.settings.providers as provcfg
from meshpipeline.adapters.model_inference.tracing import get_async_openai


def _deepinfra_timeout():
    import httpx
    return httpx.Timeout(
        provcfg.DEEPINFRA_CALL_TIMEOUT,
        connect=provcfg.DEEPINFRA_CONNECT_TIMEOUT,
        read=provcfg.DEEPINFRA_READ_TIMEOUT,
        write=provcfg.DEEPINFRA_WRITE_TIMEOUT,
        pool=provcfg.DEEPINFRA_CONNECT_TIMEOUT,
    )


def get_deepinfra_client():
    if not provcfg.DEEPINFRA_API_KEY:
        from meshpipeline.settings.env import ConfigurationError
        raise ConfigurationError(
            "DEEPINFRA_API_KEY is not set, but a route targets the deepinfra provider. Set it, or "
            "point the route elsewhere. (Startup validation reports this for the enabled profile.)")
    return get_async_openai(
        api_key=provcfg.DEEPINFRA_API_KEY, base_url=provcfg.DEEPINFRA_BASE_URL,
        timeout=_deepinfra_timeout(),
    )


BUILDER_CALL_KWARGS: dict = {
    "model":       bcfg.BUILDER_MODEL,
    "temperature": bcfg.BUILDER_TEMPERATURE,
    "max_tokens":  bcfg.BUILDER_MAX_TOKENS,
    "top_p":       bcfg.BUILDER_TOP_P,
    # min_p floor - a probability floor that keeps the builder/planner sampling consistent, not
    # spiraly, on top of the conservative BUILDER_TEMPERATURE default. top-level on
    # DeepInfra's OpenAI endpoint (it forwards to the vLLM/SGLang sampler).
    "extra_body":  {"min_p": bcfg.BUILDER_MIN_P},
    "stream":      True,
    "stream_options": {"include_usage": True},
}

# The snappy PLANNER's call kwargs - INDEPENDENT of the builder's. Reads PLANNER_* settings,
# so a BUILDER_* change never moves the planner's sampling. Same shape (GLM streaming) as the
# builder, deliberately, but decoupled.
def _planner_call_kwargs() -> dict:
    import meshpipeline.engines.snappy.settings as pcfg
    return {
        "model":       pcfg.PLANNER_MODEL,
        "temperature": pcfg.PLANNER_TEMPERATURE,
        "max_tokens":  pcfg.PLANNER_MAX_TOKENS,
        "top_p":       pcfg.PLANNER_TOP_P,
        "extra_body":  {"min_p": pcfg.PLANNER_MIN_P},
        "stream":      True,
        "stream_options": {"include_usage": True},
    }


# Reviewer = Qwen3-VL Thinking: temp 0.6 / top_p 0.95 / top_k 20 / presence 0.0
# (Qwen's own recommended Thinking params). top_k is not an OpenAI top-level
# field, so it travels via extra_body to DeepInfra.
REVIEWER_CALL_KWARGS: dict = {
    "model":            rcfg.REVIEWER_MODEL,
    "temperature":      rcfg.REVIEWER_TEMPERATURE,
    "max_tokens":       rcfg.REVIEWER_MAX_TOKENS,
    "top_p":            rcfg.REVIEWER_TOP_P,
    "presence_penalty": rcfg.REVIEWER_PRESENCE_PENALTY,
    "stream":           True,
    "stream_options":   {"include_usage": True},
    "extra_body":       {"top_k": rcfg.REVIEWER_TOP_K},
}
