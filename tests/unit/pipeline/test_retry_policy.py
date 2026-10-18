# Responsibility: Verify the builder is retried only for failures a retry can change, and that the stop is said.
from __future__ import annotations

import pytest
from langgraph.graph import END

import meshpipeline.agents.builder.settings as bcfg
from meshpipeline.application.final_result import TerminalStatus, build_final_result, render_message
from meshpipeline.contracts.failure_cause import RETRY_SKIPPED_NOTE, FailureCause
from meshpipeline.pipeline.graph import route_after_executor


def _failed(cause: str = "", attempt: int = 0, **facts) -> dict:
    return {"executor_success": False, "retry_count": attempt, "job_id": "j",
            "executor_failure_cause": cause, "executor_failure_facts": facts}


@pytest.mark.parametrize("cause", [FailureCause.CONTRACT_MISMATCH, FailureCause.GEOMETRY_REJECTED])
def test_a_failure_no_retry_can_change_ends_on_the_first_attempt(cause):
    # job ac1daa3e spent a second 13-minute mesh on a naming mismatch
    assert route_after_executor(_failed(cause, attempt=0)) == END


@pytest.mark.parametrize("cause", [FailureCause.MESH_QUALITY, FailureCause.CELL_BUDGET,
                                   FailureCause.UNDER_RESOLVED, FailureCause.PATCH_NOT_CAPTURED,
                                   FailureCause.ENGINE_CRASHED, FailureCause.NOT_SOLVABLE,
                                   FailureCause.DOMAIN_EXTENT, FailureCause.BOUNDARY_TYPE,
                                   FailureCause.REGION_SPLIT])
def test_a_meshing_failure_keeps_its_retries(cause):
    for attempt in range(bcfg.MAX_BUILDER_RETRIES + 1):
        assert route_after_executor(_failed(cause, attempt=attempt)) == "node_classifier"
    assert route_after_executor(_failed(cause, attempt=bcfg.MAX_BUILDER_RETRIES + 1)) == END


def test_an_unnamed_failure_keeps_the_old_ladder():
    assert route_after_executor(_failed("", attempt=0)) == "node_classifier"
    assert route_after_executor({"executor_success": False, "retry_count": 0}) == "node_classifier"


def test_names_the_builder_wrote_can_be_fixed_by_another_attempt():
    state = _failed(FailureCause.CONTRACT_MISMATCH, attempt=0, retry_may_fix=True)
    assert route_after_executor(state) == "node_classifier"


def test_a_passing_mesh_is_untouched_by_the_policy():
    assert route_after_executor({"executor_success": True, "retry_count": 0,
                                 "executor_failure_cause": ""}) == "node_reviewer"


def _terminal(cause, facts, gate="patch_contract", attempts=0):
    return build_final_result(
        job_id="j", owner_id="o", status=TerminalStatus.failed, engine="snappy",
        purpose="external_cfd", dimensionality="3D", approved_snapshot_id="s",
        executor_success=False, reviewer_verdict="", failed_gate=gate, api_failure="",
        attempts=attempts, attempts_max=4, required_ready=False, delivered_types=[],
        optional_warnings=[], failure_cause=cause, failure_facts=facts)


def test_the_final_message_says_the_retry_was_skipped_and_why():
    fr = _terminal(FailureCause.CONTRACT_MISMATCH, {"missing": ["car wall"],
                                                    "renamed": {"car wall": "car_wall"}})
    assert fr.retry_skipped is True
    assert RETRY_SKIPPED_NOTE in render_message(fr)


def test_a_retried_failure_does_not_claim_a_skip():
    fr = _terminal(FailureCause.MESH_QUALITY, {}, gate="quality_floor", attempts=3)
    assert fr.retry_skipped is False
    assert RETRY_SKIPPED_NOTE not in render_message(fr)
