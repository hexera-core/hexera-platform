# Responsibility: Retrieve results from a SearXNG instance.
# Boundaries: retrieval and result normalisation; distillation belongs to the summarizer.
from __future__ import annotations

from meshpipeline.adapters.search._http import request_json
from meshpipeline.contracts.search import (
    SearchResponse,
    WebSearchInvalidResponse,
    normalize_results,
)


class SearxngProvider:
    def __init__(self, *, base_url: str, timeout_s: float) -> None:
        self._url = base_url.rstrip("/") + "/search"
        self._timeout_s = timeout_s

    def search(self, *, query: str, max_results: int) -> SearchResponse:
        data = request_json(
            "GET", self._url,
            timeout_s=self._timeout_s, provider="searxng",
            params={"q": query, "format": "json", "safesearch": "0"},
        )
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise WebSearchInvalidResponse("searxng response missing a 'results' list")
        return SearchResponse(
            query=query, provider="searxng",
            results=normalize_results(data["results"], max_results=max_results),
        )
