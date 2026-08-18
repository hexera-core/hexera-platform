# Responsibility: Verify the final result names the executed engine, claims no unprovable download, and blames nobody.
from __future__ import annotations

import pytest

from meshpipeline.application.final_result import (
    FINAL_RESULT_SCHEMA_VERSION,
    FailureCategory,
    FinalResult,
    TerminalStatus,
    build_final_result,
    render_message,
)


def _build(**over):
    base = {"job_id": "j", "owner_id": "o", "status": TerminalStatus.succeeded,
            "engine": "cfmesh", "purpose": "external_cfd", "dimensionality": "3D",
            "approved_snapshot_id": "snap", "executor_success": True, "reviewer_verdict": "PASS",
            "failed_gate": "", "api_failure": "", "attempts": 1, "attempts_max": 5,
            "required_ready": True, "delivered_types": ["mesh_bundle"], "optional_warnings": []}
    base.update(over)
    return build_final_result(**base)


# success

def test_success_is_truthful_and_names_the_engine():
    fr = _build()
    msg = render_message(fr)
    assert "completed successfully" in msg and "cfmesh" in msg
    assert "ready to download" in msg and "Review: passed" in msg
    assert fr.outcome_code == "success" and fr.failure_category is None


def test_success_without_ready_artifact_does_not_claim_a_download():
    fr = _build(required_ready=False, delivered_types=[])
    msg = render_message(fr)
    assert "ready to download" not in msg     # never over-claims
    assert "prepared" in msg


def test_optional_preview_failure_is_a_warning_not_a_url():
    fr = _build(required_ready=True, delivered_types=["mesh_bundle"], optional_warnings=["mesh"])
    msg = render_message(fr)
    assert "completed successfully" in msg
    assert "optional preview" in msg and "http" not in msg.lower()


# failure paths never sound like success

def test_executor_failure_reports_failure_and_no_download():
    fr = _build(status=TerminalStatus.failed, executor_success=False, reviewer_verdict="",
                required_ready=False, delivered_types=[], attempts=5)
    msg = render_message(fr)
    assert "did not complete successfully" in msg
    assert "No downloadable mesh deliverable is available." in msg
    assert "success" not in msg.lower().split("did not")[0]
    assert fr.failure_category == FailureCategory.attempts_exhausted.value
    assert fr.reviewer_verdict is None


def test_reviewer_rejection_reports_review_failed_no_success_claim():
    fr = _build(status=TerminalStatus.failed, executor_success=True, reviewer_verdict="FAIL",
                required_ready=False, delivered_types=[])
    assert fr.failure_category == FailureCategory.review_rejected.value
    assert fr.reviewer_verdict == "failed"
    msg = render_message(fr)
    assert "did not pass review" in msg and "download" in msg.lower()
    assert "completed successfully" not in msg


def test_a_stale_pass_on_a_failed_run_never_counts_as_review_passed():
    fr = _build(status=TerminalStatus.failed, executor_success=False, reviewer_verdict="PASS",
                required_ready=False, delivered_types=[])
    assert fr.reviewer_verdict is None        # executor_success False → PASS discarded
    assert render_message(fr).count("Review: passed") == 0


def test_delivery_failure_distinguishes_execution_from_delivery():
    fr = _build(status=TerminalStatus.failed, executor_success=True, reviewer_verdict="PASS",
                required_ready=False, delivered_types=[])
    assert fr.failure_category == FailureCategory.delivery_failed.value
    msg = render_message(fr)
    assert "built and reviewed" in msg and "could not be stored" in msg
    assert "our fault" in msg and "no download" in msg.lower()


def test_gate_failure_is_gate_failed_and_patch_contract_flag():
    fr = _build(status=TerminalStatus.failed, executor_success=False, reviewer_verdict="",
                failed_gate="patch_contract", required_ready=False, delivered_types=[])
    assert fr.failure_category == FailureCategory.gate_failed.value
    assert fr.patch_contract_ok is False
    # never claims boundaries were preserved
    assert "preserved" not in render_message(fr)


def test_a_non_patch_gate_failure_leaves_patch_contract_ok_true():
    fr = _build(status=TerminalStatus.failed, executor_success=False, reviewer_verdict="",
                failed_gate="sicn_floor", required_ready=False, delivered_types=[])
    assert fr.patch_contract_ok is True


# engine truthfulness

def test_the_engine_is_the_executed_engine_only():
    fr = _build(engine="cfmesh")
    assert fr.engine == "cfmesh"
    assert "cfmesh" in render_message(fr) and "gmsh" not in render_message(fr)


# no secrets / paths / keys in any rendered message

def test_rendered_message_never_leaks_infrastructure_detail():
    for fr in (_build(),
               _build(status=TerminalStatus.failed, executor_success=False, reviewer_verdict="",
                      required_ready=False, delivered_types=[]),
               _build(status=TerminalStatus.failed, executor_success=True, reviewer_verdict="PASS",
                      required_ready=False, delivered_types=[])):
        msg = render_message(fr)
        for bad in ("jobs/", ".tar.gz", "minio", "postgres", "Traceback", "sk-", "http"):
            assert bad.lower() not in msg.lower(), f"{bad!r} leaked into: {msg}"


# durable round-trip (restart safety)

def test_round_trip_reproduces_the_same_verdict():
    for fr in (_build(),
               _build(status=TerminalStatus.failed, executor_success=True, reviewer_verdict="FAIL",
                      required_ready=False, delivered_types=[])):
        again = FinalResult.from_dict(fr.to_dict())
        assert render_message(again) == render_message(fr)
        assert again.to_dict() == fr.to_dict()


def test_a_non_current_schema_version_is_refused_future_or_malformed():
    for bad in (FINAL_RESULT_SCHEMA_VERSION + 1, None, 0, -1, "2", 2):
        d = _build().to_dict()
        d["schema_version"] = bad
        with pytest.raises(ValueError, match="schema_version"):
            FinalResult.from_dict(d)



def test_a_non_current_schema_version_is_refused():
    d = _build().to_dict()
    d["schema_version"] = 2
    with pytest.raises(ValueError, match="schema_version"):
        FinalResult.from_dict(d)


def test_failure_categories_never_blame_the_user_for_infrastructure():
    for cat in (FailureCategory.delivery_failed, FailureCategory.internal_pipeline_failure,
                FailureCategory.native_execution_failed):
        msg = render_message(FinalResult(
            schema_version=FINAL_RESULT_SCHEMA_VERSION, job_id="j", owner_id="o",
            status=TerminalStatus.failed, failure_category=cat.value, outcome_code=cat.value))
        assert "your fault" not in msg.lower()
