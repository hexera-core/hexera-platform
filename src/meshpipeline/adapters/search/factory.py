# Responsibility: Build the web-search provider the deployment is configured for.
# Boundaries: selection only.
from __future__ import annotations

import meshpipeline.settings.providers as provcfg
from meshpipeline.contracts.search import WebSearchError, WebSearchProvider

_instance: WebSearchProvider | None = None


def build_web_search_provider() -> WebSearchProvider:
    global _instance
    if _instance is None:
        backend = provcfg.WEB_SEARCH_PROVIDER
        if backend == "searxng":
            from meshpipeline.adapters.search.searxng import SearxngProvider
            _instance = SearxngProvider(
                base_url=provcfg.WEB_SEARCH_BASE_URL, timeout_s=provcfg.WEB_SEARCH_TIMEOUT)
        elif backend == "tavily":
            from meshpipeline.adapters.search.tavily import TavilyProvider
            _instance = TavilyProvider(
                api_key=provcfg.TAVILY_API_KEY, timeout_s=provcfg.WEB_SEARCH_TIMEOUT)
        else:
            raise WebSearchError(
                f"unknown WEB_SEARCH_PROVIDER {backend!r} (expected searxng|tavily)")
    return _instance


def _reset_for_tests() -> None:
    global _instance
    _instance = None
