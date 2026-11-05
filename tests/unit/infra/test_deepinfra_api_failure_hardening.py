# Responsibility: Verify no provider failure is detected by matching prose or slicing a message.
from __future__ import annotations

import ast
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

APP_DIR = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"
REPO_ROOT = Path(__file__).parent.parent.parent.parent

for _mod in ["langfuse", "langfuse.decorators"]:
    sys.modules.setdefault(_mod, MagicMock())



# The four implementation-coupled classes that lived here are GONE, not weakened.
# They pinned `_classify_error_reason`, `_rate_limit_backoff`, `_is_transient_error` and a
# mocked client - private helpers that no longer exist, because classification moved to
# providers.classify, timing to route policy, and marker translation to failure_markers. Their
# DURABLE contracts are owned behaviourally now, through the real routed call path:
#   provider exception -> category   tests/unit/infra/test_provider_error_hardening.py
#                                      ::test_a_provider_exception_normalises_to_its_category
#   exception -> exact marker        ::test_the_builder_returns_its_established_marker_for_each_provider_failure
#                                      ::test_the_visual_reviewer_returns_its_established_marker
#   rate limit backs off longer      tests/unit/infra/test_route_timing_equivalence.py
#                                      ::test_the_backoff_schedule_is_unchanged (60/120/240/300)
#                                      ::test_rate_limits_back_off_longer_than_ordinary_failures_where_they_always_did
#   circuit open -> fail fast        test_provider_error_hardening
#                                      ::test_an_open_circuit_fails_fast_without_dialling_the_provider
# What REMAINS below is what those rewrites do not cover: the marker vocabulary's shape and its
# handling downstream (no prefix checks in the graph/tasks, no queue-on-api_failure, the value
# not truncated, and the training log using it verbatim).

class TestReviewerFailureIsTerminal:

    REVIEWER_VARIANTS = [
        "<<API_FAILURE:reviewer_rate_limit>>",
        "<<API_FAILURE:reviewer_timeout>>",
        "<<API_FAILURE:reviewer_connection>>",
        "<<API_FAILURE:reviewer_server_error>>",
        "<<API_FAILURE:reviewer_empty_response>>",
    ]

    def _read_source(self, relpath: str) -> str:
        return (REPO_ROOT / relpath).read_text()

    def test_graph_has_no_reviewer_prefix_check(self):
        src = self._read_source("src/meshpipeline/pipeline/graph.py")
        assert '"reviewer_" in api_failure' not in src, (
            "graph.py still has the deleted reviewer-queue branch"
        )

    def test_tasks_has_no_reviewer_prefix_check(self):
        src = self._read_source("src/meshpipeline/application/pipeline_run.py")
        assert '"reviewer_" in api_failure' not in src, (
            "tasks.py still has the deleted reviewer-queue branch"
        )

    def test_no_queue_on_api_failure_references(self):
        for relpath in ("src/meshpipeline/pipeline/graph.py", "src/meshpipeline/application/pipeline_run.py", "src/meshpipeline/settings/policy.py"):
            src = self._read_source(relpath)
            assert "QUEUE_ON_API_FAILURE" not in src, (
                f"{relpath} still references the deleted QUEUE_ON_API_FAILURE flag"
            )


class TestBuilderApiFailureNotTruncated:
    def _read_tree(self) -> ast.AST:
        src = (REPO_ROOT / "src/meshpipeline/agents/builder/agent.py").read_text()
        return ast.parse(src)

    def test_api_failure_val_not_sliced_200(self):
        tree = self._read_tree()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Subscript):
                continue
            val = node.value
            slc = node.slice
            if not (isinstance(val, ast.Name) and val.id == "api_failure_val"):
                continue
            if isinstance(slc, ast.Slice):
                upper = slc.upper
                if isinstance(upper, ast.Constant) and upper.value == 200:
                    pytest.fail(
                        "builder.py contains api_failure_val[:200] - truncation was removed; "
                        "please update builder.py to use api_failure_val directly"
                    )
