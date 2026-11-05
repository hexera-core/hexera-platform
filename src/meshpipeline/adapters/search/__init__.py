# Responsibility: Expose the web-search surface - the contract's types and errors plus the provider builder.
# Boundaries: re-export only; the concrete provider is chosen by the factory.
from meshpipeline.adapters.search.factory import build_web_search_provider
from meshpipeline.contracts.search import (
    SearchResponse,
    SearchResult,
    WebSearchError,
    WebSearchInvalidResponse,
    WebSearchProvider,
    WebSearchTimeout,
    WebSearchUnavailable,
)

__all__ = [
    "SearchResult", "SearchResponse", "WebSearchProvider", "WebSearchError",
    "WebSearchTimeout", "WebSearchUnavailable", "WebSearchInvalidResponse",
    "build_web_search_provider",
]
