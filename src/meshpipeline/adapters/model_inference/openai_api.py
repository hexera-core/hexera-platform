# Responsibility: Build the OpenAI client a route targeting the openai provider calls through.
# Boundaries: client construction and credentials; no routing, no retries, no sampling policy.
from __future__ import annotations

import meshpipeline.settings.providers as provcfg
from meshpipeline.adapters.model_inference.tracing import get_async_openai


def get_openai_client():
    if not provcfg.OPENAI_API_KEY:
        from meshpipeline.settings.env import ConfigurationError
        raise ConfigurationError(
            "OPENAI_API_KEY is not set, but a route targets the openai provider. Set it, or "
            "point the route elsewhere. (Startup validation reports this for the enabled profile.)")
    return get_async_openai(api_key=provcfg.OPENAI_API_KEY, base_url=provcfg.OPENAI_BASE_URL)
