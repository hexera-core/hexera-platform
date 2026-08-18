# Responsibility: Verify the real graph's nodes and every routing decision after builder, executor and reviewer.
from __future__ import annotations

from unittest.mock import patch

import pytest

# pyvista/PIL stand-ins are installed ONCE by tests/unit/conftest.py, and only where the
# real package is genuinely absent. Doing it per-module raced: whichever module was
# imported first decided, and one of them shadowed an installed pyvista.
from langgraph.graph import END

import meshpipeline.agents.builder.settings as bcfg
from meshpipeline.pipeline.graph import (
    build_graph,
    node_failure_handler,
    route_after_builder,
    route_after_executor,
    route_after_reviewer,
)


def test_build_graph_rejects_none_checkpointer():
    with pytest.raises(ValueError, match="checkpointer is required"):
        build_graph(checkpointer=None)


def test_build_graph_calls_all_required_nodes():
    import meshpipeline.pipeline.graph as _graph_mod

    class _RecordingGraph:
        def __init__(self, *a, **k):
            self.nodes = set()
            self.edges = []
            self.cond_edges = []
        def add_node(self, name, fn=None):
            self.nodes.add(name)
        def add_edge(self, src, dst):
            self.edges.append((src, dst))
        def add_conditional_edges(self, src, fn, mapping):
            self.cond_edges.append(src)
        def compile(self, **k):
            return self

    recorder = _RecordingGraph()
    with patch.object(_graph_mod, "StateGraph", return_value=recorder):
        _graph_mod.build_graph(checkpointer=object())

    expected_nodes = {
        "node_intake", "node_builder", "node_executor",
        "node_classifier", "node_reviewer", "node_failure_handler",
    }
    assert expected_nodes.issubset(recorder.nodes), (
        f"Missing nodes: {expected_nodes - recorder.nodes}"
    )



def test_route_after_builder_no_failure_goes_to_executor():
    assert route_after_builder({"api_failure": ""}) == "node_executor"
    assert route_after_builder({}) == "node_executor"


def test_route_after_builder_with_api_failure_goes_to_failure_handler():
    assert route_after_builder({"api_failure": "builder_timeout"}) == "node_failure_handler"



def test_route_after_executor_success_goes_to_reviewer():
    state = {"executor_success": True, "retry_count": 1}
    assert route_after_executor(state) == "node_reviewer"


def test_route_after_executor_first_failure_goes_to_classifier():
    state = {"executor_success": False, "retry_count": 1}
    with patch.object(bcfg, "MAX_BUILDER_RETRIES", 3):
        result = route_after_executor(state)
    assert result == "node_classifier"


def test_route_after_executor_budget_exhausted_non_solvability_ends_the_graph():
    # A mesh that exhausted all attempts WITHOUT ever passing the executor gates
    # (manifest/contract/solvability) is NOT deliverable - it must go terminally to
    # END, never to the reviewer. Routing an un-validated mesh to the
    # reviewer is how a text-only PASS could ship an unchecked mesh.
    state = {"executor_success": False, "retry_count": 4, "solvability_failed": False}
    with patch.object(bcfg, "MAX_BUILDER_RETRIES", 3):
        result = route_after_executor(state)
    assert result == END


def test_route_after_executor_exhausted_and_unsolvable_ends_the_graph():
    state = {
        "executor_success": False,
        "retry_count": 4,
        "solvability_failed": True,
        "job_id": "x",
    }
    with patch.object(bcfg, "MAX_BUILDER_RETRIES", 3):
        result = route_after_executor(state)
    assert result == END, (
        "exhausted+unsolvable must end the graph - reviewer is a "
        "visual-quality check and cannot rescue an unrunnable mesh"
    )


def test_route_after_executor_at_limit_boundary_goes_to_classifier():
    state = {"executor_success": False, "retry_count": 3}
    with patch.object(bcfg, "MAX_BUILDER_RETRIES", 3):
        result = route_after_executor(state)
    assert result == "node_classifier"



def test_route_after_reviewer_pass_ends_the_graph():
    state = {"reviewer_verdict": "PASS", "retry_count": 1, "api_failure": ""}
    assert route_after_reviewer(state) == END


def test_route_after_reviewer_fail_with_budget_goes_to_classifier():
    state = {"reviewer_verdict": "FAIL", "retry_count": 1, "api_failure": "", "executor_success": False}
    with patch.object(bcfg, "MAX_BUILDER_RETRIES", 3):
        result = route_after_reviewer(state)
    assert result == "node_classifier"


def test_route_after_reviewer_fail_budget_exhausted_ends_the_graph():
    state = {"reviewer_verdict": "FAIL", "retry_count": 5, "api_failure": "", "executor_success": False}
    with patch.object(bcfg, "MAX_BUILDER_RETRIES", 3):
        result = route_after_reviewer(state)
    assert result == END


def test_route_after_reviewer_extended_retry_for_executor_success():
    state = {
        "reviewer_verdict": "FAIL",
        "retry_count": 4,
        "api_failure": "",
        "executor_success": True,
    }
    with patch.object(bcfg, "MAX_BUILDER_RETRIES", 3):
        result = route_after_reviewer(state)
    assert result == "node_classifier"


def test_route_after_reviewer_extended_retry_not_triggered_without_executor_success():
    state = {
        "reviewer_verdict": "FAIL",
        "retry_count": 4,
        "api_failure": "",
        "executor_success": False,
    }
    with patch.object(bcfg, "MAX_BUILDER_RETRIES", 3):
        result = route_after_reviewer(state)
    assert result == END


def test_route_after_reviewer_api_failure_goes_to_failure_handler():
    state = {"reviewer_verdict": "PASS", "retry_count": 1, "api_failure": "reviewer_timeout"}
    assert route_after_reviewer(state) == "node_failure_handler"



async def test_failure_handler_returns_unavailability_message_for_reviewer_failure():
    state = {"api_failure": "reviewer_evidence_missing", "job_id": "test-job"}
    result = await node_failure_handler(state)
    msg = result["outcome_message"].lower()
    # blameless system-failure message: tells the user to retry, blames our side,
    # and never frames it as a mesh verdict
    assert "try again" in msg and "our side" in msg
    assert "verdict" not in msg


async def test_failure_handler_returns_unavailability_message_for_builder_failure():
    state = {"api_failure": "builder_timeout", "job_id": "test-job"}
    result = await node_failure_handler(state)
    msg = result["outcome_message"].lower()
    assert "try again" in msg and "our side" in msg
    assert "outcome_message" in result
