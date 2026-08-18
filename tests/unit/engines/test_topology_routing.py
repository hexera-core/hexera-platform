# Responsibility: Verify a flow topology resolves to a capable engine stably, and a pin outranks the purpose.
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import meshpipeline.settings.runtime as rtcfg

APP = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"

from meshpipeline.pipeline.engine_select import node_engine_select  # noqa: E402


def test_internal_purpose_resolves_to_a_capable_engine(monkeypatch, tmp_path):
    monkeypatch.setattr(rtcfg, "CORPUS_DIR", str(tmp_path))
    from meshpipeline.engines.registry import default_engine, engines_producing_topology
    assert default_engine() in engines_producing_topology("internal")
    out = asyncio.run(node_engine_select({"job_id": "t", "purpose": "internal_cfd"}))
    assert out == {"engine": default_engine()}


def test_internal_pick_is_stable_not_positional(monkeypatch, tmp_path):
    monkeypatch.setattr(rtcfg, "CORPUS_DIR", str(tmp_path))
    import meshpipeline.pipeline.engine_select as es
    monkeypatch.setattr(es, "engines_producing_topology", lambda *_: ["zeta", "alpha"])
    monkeypatch.setattr(es, "default_engine", lambda: "not_a_candidate")
    out = asyncio.run(node_engine_select({"job_id": "t", "purpose": "internal_cfd"}))
    assert out == {"engine": "alpha"}


def test_external_purpose_resolves_to_default(monkeypatch, tmp_path):
    monkeypatch.setattr(rtcfg, "CORPUS_DIR", str(tmp_path))
    out = asyncio.run(node_engine_select({"job_id": "t", "purpose": "external_cfd"}))
    assert out == {"engine": "cfmesh"}


def test_pin_beats_purpose(monkeypatch, tmp_path):
    monkeypatch.setattr(rtcfg, "CORPUS_DIR", str(tmp_path))
    out = asyncio.run(node_engine_select(
        {"job_id": "t", "engine": "snappy", "purpose": "internal_cfd"}))
    assert out == {}


def test_unknown_pinned_engine_is_rejected_not_silently_meshed(monkeypatch, tmp_path):
    monkeypatch.setattr(rtcfg, "CORPUS_DIR", str(tmp_path))
    import pytest

    from meshpipeline.engines.registry import UnknownEngineError
    with pytest.raises(UnknownEngineError):
        asyncio.run(node_engine_select({"job_id": "t", "engine": "pointwise"}))


def test_builder_and_executor_key_off_the_neutral_flow_topology():
    driver_src = (APP / "engines" / "snappy" / "drivers.py").read_text()
    executor_src = (APP / "pipeline" / "executor.py").read_text()
    graph_src = (APP / "pipeline/graph.py").read_text()
    for src in (driver_src, executor_src):
        assert 'flow_topology' in src and '"internal"' in src
        # NOT read out of engine_params any more
        assert '("topology") == "internal"' not in src.replace(".get", "")
        assert 'state.get("internal_flow")' not in src
    # builder itself no longer branches on engine/topology - the spec's build_driver seam owns the
    # dispatch, and since that seam is read by `invoke`, not by the graph node.
    builder_src = (APP / "agents" / "builder" / "invoke.py").read_text()
    assert "build_driver" in builder_src
    node_src = (APP / "agents" / "builder" / "agent.py").read_text()
    assert "build_driver" not in node_src, "the graph node dispatches on the engine again"
    assert 'if _engine == "snappy"' not in builder_src
    assert "internal_flow" not in graph_src
    assert "domain_gate" not in graph_src


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
