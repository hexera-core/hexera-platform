# Responsibility: Verify engine selection honours a user pin and otherwise resolves from capability, never from a model.
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import meshpipeline.settings.runtime as rtcfg

APP = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"

from meshpipeline.engines import registry as cat  # noqa: E402
from meshpipeline.pipeline.engine_select import node_engine_select  # noqa: E402


def _no_llm(monkeypatch):
    import meshpipeline.adapters.model_inference.router as r

    async def _boom(*a, **k):
        raise AssertionError("engine resolution must never call a model")
    monkeypatch.setattr(r, "call_classifier_model", _boom, raising=False)


def test_catalog_basics_unchanged():
    import pytest
    assert set(cat.engine_names()) == {"cfmesh", "snappy", "gmsh", "snappy_multiregion", "vmtk"}
    assert cat.default_engine() == "cfmesh"
    assert cat.get_spec("").name == "cfmesh"             # UNSET → default
    with pytest.raises(cat.UnknownEngineError):          # UNKNOWN → raises
        cat.get_spec("bogus")
    menu = cat.catalog_menu()
    # The menu carries ONLY the name a user should see - intake is never shown a registry key.
    assert "cfMesh" in menu and "snappyHexMesh" in menu
    assert "internal key" not in menu


def test_user_pin_is_kept_verbatim(monkeypatch, tmp_path):
    monkeypatch.setattr(rtcfg, "CORPUS_DIR", str(tmp_path))
    _no_llm(monkeypatch)
    out = asyncio.run(node_engine_select({"engine": "snappy", "job_id": "t"}))
    assert out == {}                       # pin untouched


def test_dispute_pin_is_kept(monkeypatch, tmp_path):
    monkeypatch.setattr(rtcfg, "CORPUS_DIR", str(tmp_path))
    _no_llm(monkeypatch)
    out = asyncio.run(node_engine_select({"engine": "cfmesh", "job_id": "t",
                                          "user_dispute": {"of_job_id": "x"}}))
    assert out == {}


def test_internal_purpose_without_pin_resolves_from_capability(monkeypatch, tmp_path):
    monkeypatch.setattr(rtcfg, "CORPUS_DIR", str(tmp_path))
    _no_llm(monkeypatch)
    out = asyncio.run(node_engine_select({"job_id": "t", "purpose": "internal_cfd"}))
    assert out == {"engine": cat.default_engine()}
    assert out["engine"] in cat.engines_producing_topology("internal")


def test_no_declaration_resolves_to_default_never_a_model(monkeypatch, tmp_path):
    monkeypatch.setattr(rtcfg, "CORPUS_DIR", str(tmp_path))
    _no_llm(monkeypatch)
    out = asyncio.run(node_engine_select({"job_id": "t", "request_txt": "mesh it"}))
    assert out == {"engine": cat.default_engine()}


def test_no_selector_llm_machinery_remains():
    src = (APP / "pipeline" / "engine_select.py").read_text()
    assert "call_classifier_model" not in src
    assert "SELECTOR_TEMPERATURE" not in src
    assert "_SELECTOR_SYSTEM" not in src


def test_graph_wires_the_resolution_node():
    src = (APP / "pipeline/graph.py").read_text()
    assert "from meshpipeline.pipeline.engine_select import node_engine_select" in src
    assert '"node_engine_select"' in src


# These nodes publish through the ownership-checked port. The suites here exercise node
# contracts, not Redis or PostgreSQL, so the port is stood in for; ownership has its own suites.
@pytest.fixture(autouse=True)
def _execution_publisher(monkeypatch):
    from tests.execution_publisher_double import install

    import meshpipeline.application.execution_publisher as _ep
    made = install(monkeypatch, _ep)
    import meshpipeline.pipeline.executor as _ex
    if hasattr(_ex, "execution_publisher"):
        monkeypatch.setattr(_ex, "execution_publisher", _ep.execution_publisher)
    return made
