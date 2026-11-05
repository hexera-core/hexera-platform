# Responsibility: Build the DeepInfra client the builder and reviewer call through.
# Boundaries: client construction and credentials; no routing, no retries.
from __future__ import annotations

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
