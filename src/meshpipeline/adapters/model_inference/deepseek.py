# Responsibility: Build the DeepSeek client intake and the search summarizer call through.
# Boundaries: client construction and credentials; no routing, no retries.
from __future__ import annotations

import meshpipeline.agents.intake.settings as icfg
import meshpipeline.settings.providers as provcfg
from meshpipeline.adapters.model_inference.tracing import get_async_openai

_THINKING_OFF = {"thinking": {"type": "disabled"}}

def get_deepseek_client():
    if not provcfg.DEEPSEEK_API_KEY:
        from meshpipeline.settings.env import ConfigurationError
        raise ConfigurationError(
            "DEEPSEEK_API_KEY is not set, but a route targets the deepseek provider. Set it, or "
            "point the route elsewhere. (Startup validation reports this for the enabled profile.)")
    return get_async_openai(api_key=provcfg.DEEPSEEK_API_KEY, base_url=provcfg.DEEPSEEK_BASE_URL)


# min_p floor at temp 1.0 (see cfg.*_MIN_P). DeepSeek's API accepts it without error; whether
# its backend applies it is provider-dependent (DeepInfra definitely does - see the builder).
INTAKE_CALL_KWARGS: dict = {
    "model":       provcfg.DEEPSEEK_MODEL,
    "temperature": icfg.INTAKE_TEMPERATURE,
    "max_tokens":  icfg.INTAKE_MAX_TOKENS,
    "extra_body":  {**_THINKING_OFF, "min_p": icfg.INTAKE_MIN_P},
}

# Search-distillation sub-agent: a small/fast DeepSeek model (V4-Flash) that reads
# raw search results and returns only a distilled answer. Shares the DeepSeek
# endpoint; just a cheaper model id.
SUMMARIZER_CALL_KWARGS: dict = {
    "model":       provcfg.SEARCH_SUMMARIZER_MODEL,
    "temperature": provcfg.SEARCH_SUMMARIZER_TEMPERATURE,
    "max_tokens":  provcfg.SEARCH_SUMMARIZER_MAX_TOKENS,
    "extra_body":  _THINKING_OFF,
}
