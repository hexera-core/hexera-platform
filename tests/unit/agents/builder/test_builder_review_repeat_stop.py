# Responsibility: Verify a builder stop ends the run on the review already reached, spending no attempt.
# Boundaries: node_builder's patch and the graph's route after it; the driver's detection is tested in engines/.
from __future__ import annotations

import asyncio
import types

import pytest

import meshpipeline.agents.builder.agent as agent
import meshpipeline.agents.builder.attempt_capture as attempt_capture
import meshpipeline.agents.builder.invoke as invoke
from meshpipeline.agents.builder.driver_run import STOP_REVIEWED_CASE_REPEATS
from meshpipeline.pipeline.graph import route_after_builder


@pytest.fixture(autouse=True)
def published(monkeypatch):
    from tests.execution_publisher_double import install
    return install(monkeypatch, agent)


def _notes(published) -> list[str]:
    return [c["text"] for p in published for c in p.calls if c["method"] == "anote"]


def _driver(stop: bool, called: list):
    async def _drive(workspace, state, *, job_id, publish, source_path, run):
        called.append(workspace)
        if stop:
            return False, STOP_REVIEWED_CASE_REPEATS, run.outcome(
                produced_deliverable=False, failure_marker=STOP_REVIEWED_CASE_REPEATS)
        return True, "submit_mesh:success", run.outcome(produced_deliverable=True)
    return _drive


def _run(state: dict, driver, monkeypatch) -> dict:
    class _Spec:
        build_driver = staticmethod(driver)

    # raising=False: an earlier suite in this directory removes the module's get_spec seam
    monkeypatch.setattr(invoke, "get_spec", lambda *_a, **_k: _Spec(), raising=False)
    monkeypatch.setattr(attempt_capture, "TrainingLogger",
                        lambda *a, **k: types.SimpleNamespace(log=lambda *a, **k: None))
    monkeypatch.setattr("meshpipeline.agents.loop.diagnostics.emit", lambda rec: {})
    return asyncio.run(agent.node_builder(state))


@pytest.fixture(autouse=True)
def _workspace_base(monkeypatch, tmp_path):
    import meshpipeline.settings.runtime as rtcfg
    monkeypatch.setattr(rtcfg, "WORKSPACE_BASE", tmp_path / "workspaces")


def _review_retry(tmp_path, **over) -> dict:
    reviewed = tmp_path / "reviewed_attempt"
    reviewed.mkdir()
    st = {"job_id": "j", "engine": "snappy", "retry_count": 2, "builder_mode": "retry",
          "geometry": {}, "request_txt": "r", "review_brief_txt": "b", "intake_patches": [],
          "openfoam_workspace": str(reviewed), "reviewer_verdict": "FAIL",
          "executor_success": True,
          "classifier_result": {"error_source": "reviewer_fail", "summary": "add a wake region"}}
    st.update(over)
    return st


def test_a_stop_keeps_the_attempt_and_the_reviewed_mesh(tmp_path, monkeypatch, published):
    called: list = []
    st = _review_retry(tmp_path)
    out = _run(st, _driver(True, called), monkeypatch)
    assert called, "the driver never ran"
    assert out["builder_stop"] == STOP_REVIEWED_CASE_REPEATS
    # no attempt spent: the reviewed attempt's findings stay this run's latest
    assert out["retry_count"] == 2
    # the run ends pointing at the mesh the review judged, not at the empty retry workspace
    assert out["openfoam_workspace"] == st["openfoam_workspace"]
    assert route_after_builder({**st, **out}) == "__end__"
    notes = _notes(published)
    assert notes[-1].startswith("Stopping: the review asked for changes this rebuild cannot make")
    assert not any(n.startswith("Mesh built") for n in notes)


def test_an_ordinary_build_carries_no_stop(tmp_path, monkeypatch, published):
    called: list = []
    st = _review_retry(tmp_path)
    out = _run(st, _driver(False, called), monkeypatch)
    assert "builder_stop" not in out
    assert out["retry_count"] == 3
    assert out["openfoam_workspace"] != st["openfoam_workspace"]
    assert route_after_builder({**st, **out}) == "node_executor"
    # a deterministic driver takes no tool steps: no more "Mesh built - 0 steps"
    assert _notes(published)[-1] == "Mesh built"


def test_only_a_declared_stop_marker_reaches_the_graph():
    assert invoke._DRIVER_STOPS == frozenset({STOP_REVIEWED_CASE_REPEATS})
    assert invoke.TurnOutcome(driver_stop=STOP_REVIEWED_CASE_REPEATS).repeats_reviewed_case
    assert not invoke.TurnOutcome(driver_stop="").repeats_reviewed_case


def test_the_route_ends_on_a_stop_before_anything_else():
    # even with a stale provider marker in state, a stop is a decision already made
    assert route_after_builder({"builder_stop": STOP_REVIEWED_CASE_REPEATS}) == "__end__"
    assert route_after_builder({"builder_stop": ""}) == "node_executor"


def test_the_graph_wires_an_end_after_the_builder():
    from unittest.mock import patch

    import meshpipeline.pipeline.graph as g

    class _Rec:
        def __init__(self, *a, **k):
            self.conditional: dict = {}

        def add_node(self, *a, **k):
            pass

        def add_edge(self, *a, **k):
            pass

        def add_conditional_edges(self, source, _route, mapping=None):
            self.conditional[source] = dict(mapping or {})

        def compile(self, **k):
            return self

    rec = _Rec()
    with patch.object(g, "StateGraph", return_value=rec):
        g.build_graph(checkpointer=object())
    targets = rec.conditional["node_builder"]
    # every value the route can return has an edge - a missing END would fail at run time
    assert set(targets) >= {"node_executor", "node_failure_handler", "node_infra_retry",
                            g.END}
