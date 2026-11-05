# Responsibility: Build the DeepSeek client intake and the search summarizer call through.
# Boundaries: client construction and credentials; no routing, no retries.
from __future__ import annotations

import meshpipeline.settings.providers as provcfg
from meshpipeline.adapters.model_inference.tracing import get_async_openai


def get_deepseek_client():
    if not provcfg.DEEPSEEK_API_KEY:
        from meshpipeline.settings.env import ConfigurationError
        raise ConfigurationError(
            "DEEPSEEK_API_KEY is not set, but a route targets the deepseek provider. Set it, or "
            "point the route elsewhere. (Startup validation reports this for the enabled profile.)")
    return get_async_openai(api_key=provcfg.DEEPSEEK_API_KEY, base_url=provcfg.DEEPSEEK_BASE_URL)
