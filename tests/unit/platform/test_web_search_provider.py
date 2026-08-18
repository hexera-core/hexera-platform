# Responsibility: Verify each search provider normalises into neutral records and fails closed without echoing a key.
from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

import meshpipeline.settings.providers as provcfg

APP = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"

from meshpipeline.adapters.search import (  # noqa: E402
    SearchResponse,
    SearchResult,
    WebSearchError,
    WebSearchInvalidResponse,
    WebSearchTimeout,
    WebSearchUnavailable,
    _http,  # noqa: E402
    build_web_search_provider,
    factory,  # noqa: E402
)


# a fake httpx.Client that drives request_json without touching the network
class _FakeResp:
    def __init__(self, json_data=None, *, raise_for_status=None):
        self._json = json_data
        self._raise = raise_for_status

    def raise_for_status(self):
        if self._raise is not None:
            raise self._raise

    def json(self):
        if isinstance(self._json, Exception):
            raise self._json
        return self._json


class _FakeClient:
    captured: dict = {}
    behavior = None

    def __init__(self, *args, **kwargs):
        _FakeClient.init_kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def request(self, method, url, params=None, json=None, headers=None):
        _FakeClient.captured = {"method": method, "url": url, "params": params,
                                "json": json, "headers": headers}
        beh = _FakeClient.behavior
        if isinstance(beh, Exception):
            raise beh
        return beh


@pytest.fixture(autouse=True)
def _reset():
    factory._reset_for_tests()
    _FakeClient.captured = {}
    _FakeClient.behavior = None
    yield
    factory._reset_for_tests()


@pytest.fixture
def fake_http(monkeypatch):
    monkeypatch.setattr(_http.httpx, "Client", _FakeClient)
    return _FakeClient


def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://example.invalid/search")
    return httpx.HTTPStatusError("boom", request=req, response=httpx.Response(code, request=req))


# factory selection / fail-closed
def test_factory_selects_searxng_or_tavily(monkeypatch):
    from meshpipeline.adapters.search.searxng import SearxngProvider
    from meshpipeline.adapters.search.tavily import TavilyProvider

    monkeypatch.setattr(provcfg, "WEB_SEARCH_PROVIDER", "searxng")
    assert isinstance(build_web_search_provider(), SearxngProvider)

    factory._reset_for_tests()
    monkeypatch.setattr(provcfg, "WEB_SEARCH_PROVIDER", "tavily")
    monkeypatch.setattr(provcfg, "TAVILY_API_KEY", "tvly-DUMMY")
    assert isinstance(build_web_search_provider(), TavilyProvider)


def test_unsupported_provider_fails_closed(monkeypatch):
    monkeypatch.setattr(provcfg, "WEB_SEARCH_PROVIDER", "bing")
    with pytest.raises(WebSearchError):
        build_web_search_provider()


# SearXNG behavior unchanged
def test_searxng_request_shape_and_normalization_unchanged(fake_http):
    from meshpipeline.adapters.search.searxng import SearxngProvider
    fake_http.behavior = _FakeResp({"results": [
        {"title": "cfMesh guide", "url": "https://a.example/x", "content": "use cartesianMesh"},
        {"title": "thresholds", "url": "https://b.example/y", "content": "maxNonOrtho 65"},
    ]})
    prov = SearxngProvider(base_url="http://searxng:8080", timeout_s=20)
    resp = prov.search(query="cfmesh layers", max_results=5)

    # request shape identical to the pre-adapter inline retrieval
    assert fake_http.captured["method"] == "GET"
    assert fake_http.captured["url"] == "http://searxng:8080/search"
    assert fake_http.captured["params"] == {"q": "cfmesh layers", "format": "json", "safesearch": "0"}
    # neutral records
    assert isinstance(resp, SearchResponse) and resp.provider == "searxng"
    assert [r.url for r in resp.results] == ["https://a.example/x", "https://b.example/y"]
    assert resp.results[0].snippet == "use cartesianMesh"
    assert all(isinstance(r, SearchResult) for r in resp.results)


def test_max_results_bounds_the_returned_count(fake_http):
    from meshpipeline.adapters.search.searxng import SearxngProvider
    fake_http.behavior = _FakeResp({"results": [
        {"title": f"t{i}", "url": f"https://e.example/{i}", "content": "c"} for i in range(20)
    ]})
    prov = SearxngProvider(base_url="http://searxng:8080", timeout_s=20)
    assert len(prov.search(query="q", max_results=3).results) == 3


# Tavily request + normalization
def test_tavily_request_uses_configured_query_and_bounded_count(fake_http):
    from meshpipeline.adapters.search.tavily import TavilyProvider
    fake_http.behavior = _FakeResp({"results": []})
    TavilyProvider(api_key="tvly-DUMMY", timeout_s=15).search(query="naca airfoil y+", max_results=4)
    body = fake_http.captured["json"]
    assert fake_http.captured["method"] == "POST"
    assert fake_http.captured["url"] == "https://api.tavily.com/search"
    assert body["query"] == "naca airfoil y+"
    assert body["max_results"] == 4
    # the key rides the body, never the URL/params
    assert body["api_key"] == "tvly-DUMMY"
    assert fake_http.captured["params"] is None


def test_tavily_response_normalizes_into_neutral_records(fake_http):
    from meshpipeline.adapters.search.tavily import TavilyProvider
    fake_http.behavior = _FakeResp({"results": [
        {"title": "T", "url": "https://t.example/1", "content": "snip",
         "published_date": "2025-01-02", "score": 0.87},
    ]})
    resp = TavilyProvider(api_key="tvly-DUMMY", timeout_s=15).search(query="q", max_results=5)
    r = resp.results[0]
    assert isinstance(r, SearchResult)
    assert (r.title, r.url, r.snippet) == ("T", "https://t.example/1", "snip")
    assert r.published_at == "2025-01-02"
    assert r.score == 0.87


def test_tavily_missing_key_fails_closed_without_echo():
    from meshpipeline.adapters.search.tavily import TavilyProvider
    with pytest.raises(WebSearchUnavailable):
        TavilyProvider(api_key="", timeout_s=15)


# error classification
def test_timeout_becomes_websearchtimeout(fake_http):
    from meshpipeline.adapters.search.searxng import SearxngProvider
    fake_http.behavior = httpx.ConnectTimeout("slow")
    with pytest.raises(WebSearchTimeout):
        SearxngProvider(base_url="http://searxng:8080", timeout_s=1).search(query="q", max_results=5)


def test_http_status_becomes_unavailable(fake_http):
    from meshpipeline.adapters.search.searxng import SearxngProvider
    fake_http.behavior = _FakeResp(raise_for_status=_status_error(503))
    with pytest.raises(WebSearchUnavailable) as ei:
        SearxngProvider(base_url="http://searxng:8080", timeout_s=5).search(query="q", max_results=5)
    assert "503" in str(ei.value)


def test_transport_error_becomes_unavailable(fake_http):
    from meshpipeline.adapters.search.searxng import SearxngProvider
    fake_http.behavior = httpx.ConnectError("refused")
    with pytest.raises(WebSearchUnavailable):
        SearxngProvider(base_url="http://searxng:8080", timeout_s=5).search(query="q", max_results=5)


def test_non_json_becomes_invalid_response(fake_http):
    from meshpipeline.adapters.search.searxng import SearxngProvider
    fake_http.behavior = _FakeResp(ValueError("not json"))
    with pytest.raises(WebSearchInvalidResponse):
        SearxngProvider(base_url="http://searxng:8080", timeout_s=5).search(query="q", max_results=5)


def test_missing_results_list_is_invalid_response(fake_http):
    from meshpipeline.adapters.search.searxng import SearxngProvider
    fake_http.behavior = _FakeResp({"suggestions": []})  # no 'results' list
    with pytest.raises(WebSearchInvalidResponse):
        SearxngProvider(base_url="http://searxng:8080", timeout_s=5).search(query="q", max_results=5)


# malformed entries: ignored predictably, no vendor object leaks
def test_malformed_entries_are_skipped_and_no_dict_leaks(fake_http):
    from meshpipeline.adapters.search.searxng import SearxngProvider
    fake_http.behavior = _FakeResp({"results": [
        "not-a-dict",                                        # skipped
        {"title": "no url"},                                 # skipped (no url)
        {"url": "", "content": "empty url"},                 # skipped (blank url)
        {"title": "ok", "url": "https://ok.example", "content": "good", "score": "n/a"},
    ]})
    results = SearxngProvider(base_url="http://s:8080", timeout_s=5).search(
        query="q", max_results=5).results
    assert len(results) == 1
    assert all(isinstance(r, SearchResult) for r in results)   # never a raw dict
    assert results[0].url == "https://ok.example"
    assert results[0].score is None                            # unparseable score -> None


# the API key never surfaces in an exception message
def test_api_key_absent_from_exception_text(fake_http):
    from meshpipeline.adapters.search.tavily import TavilyProvider
    secret = "tvly-SUPERSECRET-abc123"
    fake_http.behavior = _FakeResp(raise_for_status=_status_error(401))
    with pytest.raises(WebSearchUnavailable) as ei:
        TavilyProvider(api_key=secret, timeout_s=5).search(query="q", max_results=5)
    blob = str(ei.value) + repr(ei.value) + "".join(str(e) for e in _causal_chain(ei.value))
    assert secret not in blob


def _causal_chain(exc):
    seen = []
    cur = exc.__cause__
    while cur is not None and cur not in seen:
        seen.append(cur)
        cur = getattr(cur, "__cause__", None)
    return seen


# structural confinement
def test_tavily_endpoint_confined_to_tavily_module():
    for p in APP.rglob("*.py"):
        if p.name == "tavily.py":
            continue
        src = re.sub(r"#.*", "", p.read_text())
        assert "api.tavily.com" not in src, (
            f"{p.relative_to(APP)} references the Tavily endpoint - confine it to tavily.py")


def test_agents_and_pipeline_import_only_the_search_interface():
    for sub in ("agents", "pipeline", "pipeline/graph.py"):
        base = APP / sub
        files = [base] if base.is_file() else list(base.rglob("*.py"))
        for p in files:
            src = re.sub(r"#.*", "", p.read_text())
            for bad in ("web_search.searxng", "web_search.tavily", "web_search._http",
                        "api.tavily.com", "SearxngProvider", "TavilyProvider"):
                assert bad not in src, f"{p.relative_to(APP)} reaches past the search seam: {bad}"
