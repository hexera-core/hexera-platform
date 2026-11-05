# Responsibility: Verify web search returns a distilled answer and degrades to a notice rather than failing.
import pytest

from meshpipeline.agent_tools.shared import web_search as ws  # noqa: E402


@pytest.mark.asyncio
async def test_returns_only_distilled_answer(monkeypatch):
    async def fake_retrieve(query):
        return [{"title": "Gmsh docs", "url": "http://x", "content": "use setTransfiniteCurve"}]

    async def fake_distill(query, results):
        return "Use gmsh.model.mesh.setTransfiniteCurve before generate(3).", {"total_tokens": 42}

    logged = {}
    monkeypatch.setattr(ws, "_retrieve", fake_retrieve)
    monkeypatch.setattr(ws, "_distill", fake_distill)
    monkeypatch.setattr(ws, "_log_search_event",
                        lambda jid, ev, collector=None: logged.update(ev))
    monkeypatch.setattr(ws.provcfg, "WEB_SEARCH_ENABLED", True)

    out = await ws.web_search("transfinite span constraint", job_id="job1")
    assert out == "Use gmsh.model.mesh.setTransfiniteCurve before generate(3)."
    # corpus event captured the search model's signal
    assert logged["status"] == "ok"
    assert logged["usage"] == {"total_tokens": 42}
    assert logged["n_results"] == 1
    assert logged["summarizer_model"]  # provider/model recorded


@pytest.mark.asyncio
async def test_retrieval_failure_degrades_gracefully(monkeypatch):
    async def boom(query):
        raise ConnectionError("searxng down")

    monkeypatch.setattr(ws, "_retrieve", boom)
    monkeypatch.setattr(ws.provcfg, "WEB_SEARCH_ENABLED", True)
    out = await ws.web_search("anything")
    assert "unavailable" in out.lower()
    assert "ConnectionError" in out


@pytest.mark.asyncio
async def test_distill_failure_falls_back_to_raw_links(monkeypatch):
    async def fake_retrieve(query):
        return [{"title": "A", "url": "http://a", "content": "c"}]

    async def boom(query, results):
        raise RuntimeError("summarizer 500")

    monkeypatch.setattr(ws, "_retrieve", fake_retrieve)
    monkeypatch.setattr(ws, "_distill", boom)
    monkeypatch.setattr(ws.provcfg, "WEB_SEARCH_ENABLED", True)
    out = await ws.web_search("q")
    assert "http://a" in out


@pytest.mark.asyncio
async def test_disabled_returns_notice(monkeypatch):
    monkeypatch.setattr(ws.provcfg, "WEB_SEARCH_ENABLED", False)
    out = await ws.web_search("q")
    assert "disabled" in out.lower()


@pytest.mark.asyncio
async def test_empty_results_notice(monkeypatch):
    async def empty(query):
        return []

    monkeypatch.setattr(ws, "_retrieve", empty)
    monkeypatch.setattr(ws.provcfg, "WEB_SEARCH_ENABLED", True)
    out = await ws.web_search("q")
    assert "no results" in out.lower()
