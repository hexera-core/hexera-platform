# Responsibility: Retrieve results from Tavily.
# Boundaries: retrieval and result normalisation only.
from __future__ import annotations

from meshpipeline.adapters.search._http import request_json
from meshpipeline.contracts.search import (
    SearchResponse,
    WebSearchInvalidResponse,
    WebSearchUnavailable,
    normalize_results,
)

_ENDPOINT = "https://api.tavily.com/search"


class TavilyProvider:
    def __init__(self, *, api_key: str, timeout_s: float) -> None:
        # Fail fast and WITHOUT echoing anything: a missing key is a config error, not a
        # value we ever want to render.
        if not api_key:
            raise WebSearchUnavailable("tavily provider selected but TAVILY_API_KEY is empty")
        self._api_key = api_key
        self._timeout_s = timeout_s

    def search(self, *, query: str, max_results: int) -> SearchResponse:
        # api_key rides the body (never the URL/params) so it cannot surface in a logged
        # request line or an httpx status-error message.
        data = request_json(
            "POST", _ENDPOINT,
            timeout_s=self._timeout_s, provider="tavily",
            json={
                "api_key": self._api_key,
                "query": query,
                "max_results": max_results,
                "search_depth": "basic",
            },
        )
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise WebSearchInvalidResponse("tavily response missing a 'results' list")
        # Tavily hands back title/url/content plus optional published_date/score - the
        # shared normaliser maps them to the neutral record, same as SearXNG.
        return SearchResponse(
            query=query, provider="tavily",
            results=normalize_results(data["results"], max_results=max_results),
        )
