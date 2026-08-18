# Responsibility: Declare how the product searches the web, and what a result looks like.
# Boundaries: a Protocol, its errors and result normalisation; providers live in adapters/search/.
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


# errors
class WebSearchError(RuntimeError):
    pass


class WebSearchTimeout(WebSearchError):
    pass


class WebSearchUnavailable(WebSearchError):
    pass


class WebSearchInvalidResponse(WebSearchError):
    pass


# provider-neutral records
@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    published_at: str | None = None
    score: float | None = None


@dataclass(frozen=True)
class SearchResponse:
    query: str
    provider: str
    results: tuple[SearchResult, ...] = field(default_factory=tuple)


@runtime_checkable
class WebSearchProvider(Protocol):

    def search(self, *, query: str, max_results: int) -> SearchResponse: ...


# shared, deterministic normalisation
# Provider fields vary in name; map every alias to the neutral field here so BOTH
# providers produce identically-shaped records.
_PUBLISHED_KEYS = ("published_date", "publishedDate", "published_at", "publishedAt")


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _as_score(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_results(
    raw: Iterable[Any], *, max_results: int, snippet_key: str = "content",
) -> tuple[SearchResult, ...]:
    out: list[SearchResult] = []
    for entry in raw:
        if len(out) >= max_results:
            break
        if not isinstance(entry, dict):
            continue
        url = _as_text(entry.get("url")).strip()
        if not url:
            continue
        published = next(
            (_as_text(entry[k]) for k in _PUBLISHED_KEYS if entry.get(k) not in (None, "")),
            None,
        )
        out.append(SearchResult(
            title=_as_text(entry.get("title")),
            url=url,
            snippet=_as_text(entry.get(snippet_key)),
            published_at=published,
            score=_as_score(entry.get("score")),
        ))
    return tuple(out)


# runtime-injected accessor
# The concrete provider is SELECTED from config by adapters.search.build_web_search_provider and
# INJECTED here by runtime composition. Product calls get_web_search_provider() (this neutral
# accessor) - it never selects a provider through the adapter factory.
_provider: WebSearchProvider | None = None


def set_web_search_provider(provider: WebSearchProvider | None) -> None:
    global _provider
    _provider = provider


def get_web_search_provider() -> WebSearchProvider:
    if _provider is None:
        raise WebSearchError(
            "no web-search provider configured - runtime composition must call set_web_search_provider()")
    return _provider
