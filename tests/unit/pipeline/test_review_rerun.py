# Responsibility: Prove a review that ends without a verdict on a validated mesh is started again on the same mesh, and only when that can help.
# Boundaries: the graph's routing, the rerun node and the compiled wiring; the review itself is stubbed.
# The live failure (Windsor body, job 53bbce4b, 2026-09-30): a 28-minute snappyHexMesh run produced a
# 2.49M-cell mesh that passed every gate and a trial solve; the review stalled after seven rounds,
# route_after_reviewer sent every api_failure to the failure sink, and the job failed on attempt 1
# of 3 with the mesh thrown away. Nothing about the mesh was ever judged.
from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest

import meshpipeline.agents.reviewer.settings as rcfg
from meshpipeline.pipeline import graph as G

_VALIDATED = {"job_id": "j-windsor", "executor_success": True, "retry_count": 1}


@pytest.fixture(autouse=True)
def _budget():
    with patch.object(rcfg, "REVIEWER_RERUN_MAX", 2), \
            patch.object(rcfg, "REVIEWER_RERUN_BACKOFF_S", 0):
        yield


# which non-verdicts are looked at again

@pytest.mark.parametrize("marker", [
    "reviewer_stalled",               # the conversation stalled (job 53bbce4b)
    "reviewer_exhausted",             # out of rounds or its own time
    "reviewer_render_unavailable",    # the renderer fell over
    "reviewer_timeout",               # a transient provider failure
    "reviewer_rate_limit",
    "reviewer_connection",            # a dependency blip
    "<<API_FAILURE:reviewer_transient>>",
])
def test_a_review_another_look_can_change_is_started_again(marker):
    assert G.route_after_reviewer({**_VALIDATED, "api_failure": marker}) == "node_review_retry"


@pytest.mark.parametrize("marker", [
    "reviewer_evidence_missing",      # a pre-loop refusal: the same inputs refuse again
    "reviewer_non_transient",         # auth, bad request: broken, not busy
    "reviewer_circuit_open",          # the provider is down for good
])
def test_a_review_that_would_fail_the_same_way_goes_to_the_sink(marker):
    assert G.route_after_reviewer({**_VALIDATED, "api_failure": marker}) == "node_failure_handler"


def test_only_a_validated_mesh_is_reviewed_again():
    state = {**_VALIDATED, "executor_success": False, "api_failure": "reviewer_stalled"}
    assert G.route_after_reviewer(state) == "node_failure_handler"


def test_the_reruns_are_bounded_per_job():
    state = {**_VALIDATED, "api_failure": "reviewer_stalled", "review_rerun_count": 1}
    assert G.route_after_reviewer(state) == "node_review_retry"
    state["review_rerun_count"] = 2
    assert G.route_after_reviewer(state) == "node_failure_handler"


def test_the_kill_switch_turns_reruns_off():
    with patch.object(rcfg, "REVIEWER_RERUN_MAX", 0):
        assert G.route_after_reviewer(
            {**_VALIDATED, "api_failure": "reviewer_stalled"}) == "node_failure_handler"


def test_no_rerun_once_the_run_is_out_of_time():
    spent = {**_VALIDATED, "api_failure": "reviewer_stalled",
             "pipeline_deadline_epoch": time.time() - 5}
    assert G.route_after_reviewer(spent) == "node_failure_handler"
    left = {**spent, "pipeline_deadline_epoch": time.time() + 3600}
    assert G.route_after_reviewer(left) == "node_review_retry"


def test_no_rerun_when_the_wait_and_a_usable_review_no_longer_fit():
    # a transient failure waits the backoff out first; with less than that plus a usable review
    # window left, sleeping would only delay the run's own timeout
    with patch.object(rcfg, "REVIEWER_RERUN_BACKOFF_S", 90):
        near = time.time() + 90 + G.REVIEW_RERUN_MIN_WINDOW_S - 30
        transient = {**_VALIDATED, "api_failure": "reviewer_timeout",
                     "pipeline_deadline_epoch": near}
        assert G.route_after_reviewer(transient) == "node_failure_handler"
        # a stall does not wait, so the same time still fits a review
        stalled = {**transient, "api_failure": "reviewer_stalled"}
        assert G.route_after_reviewer(stalled) == "node_review_retry"
        short = {**stalled, "pipeline_deadline_epoch": time.time() + 60}
        assert G.route_after_reviewer(short) == "node_failure_handler"


def test_a_verdict_still_routes_as_before():
    assert G.route_after_reviewer({**_VALIDATED, "reviewer_verdict": "PASS"}) == G.END


# the rerun itself

def test_the_rerun_clears_the_marker_counts_itself_and_rebuilds_nothing():
    out = asyncio.run(G.node_review_retry(
        {**_VALIDATED, "api_failure": "reviewer_stalled", "review_rerun_count": 0}))
    assert out == {"api_failure": "", "review_rerun_count": 1}, \
        "a rerun must not touch retry_count - it is not a new mesh attempt"


def test_a_stalled_review_starts_again_at_once_and_a_transient_one_waits():
    slept: list[float] = []

    async def _sleep(s):
        slept.append(s)

    with patch.object(rcfg, "REVIEWER_RERUN_BACKOFF_S", 90), \
            patch("asyncio.sleep", _sleep):
        asyncio.run(G.node_review_retry({**_VALIDATED, "api_failure": "reviewer_stalled"}))
        assert slept == []
        asyncio.run(G.node_review_retry({**_VALIDATED, "api_failure": "reviewer_timeout"}))
        assert slept == [90]


def test_the_graph_wires_the_rerun_back_to_the_reviewer():
    class _RecordingGraph:
        def __init__(self, *a, **k):
            self.nodes, self.edges, self.cond = set(), [], {}

        def add_node(self, name, fn=None):
            self.nodes.add(name)

        def add_edge(self, src, dst):
            self.edges.append((src, dst))

        def add_conditional_edges(self, src, fn, mapping):
            self.cond[src] = dict(mapping)

        def compile(self, **k):
            return self

    rec = _RecordingGraph()
    with patch.object(G, "StateGraph", return_value=rec):
        G.build_graph(checkpointer=object())
    assert "node_review_retry" in rec.nodes
    assert ("node_review_retry", "node_reviewer") in rec.edges
    assert rec.cond["node_reviewer"]["node_review_retry"] == "node_review_retry"
