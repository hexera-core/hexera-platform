# Responsibility: Verify only a concluded review can be a rejection, and reviewer-side failures stay inconclusive.
from __future__ import annotations

import itertools

import pytest

from meshpipeline.application.final_result import (
    FailureCategory,
    ReviewExecution,
    ReviewVerdict,
    TerminalStatus,
    build_final_result,
    render_message,
)

# markers the reviewer itself emits when it cannot conclude (errors.py maps every one of these to
# FailureClass.REVIEW_EVIDENCE_MISSING)
REVIEWER_MARKERS = ("reviewer_evidence_missing", "reviewer_exhausted",
                    "reviewer_render_unavailable")


def fr(**kw):
    base = {"job_id": "job-1", "owner_id": "owner-1", "status": TerminalStatus.failed,
            "engine": "snappy", "purpose": "external_cfd", "dimensionality": "3D",
            "approved_snapshot_id": "snap-1", "executor_success": True, "reviewer_verdict": "",
            "failed_gate": "", "api_failure": "", "attempts": 2, "attempts_max": 5,
            "required_ready": False, "delivered_types": [], "optional_warnings": []}
    base.update(kw)
    return build_final_result(**base)


# the real DPW4 CRM regression
def test_a_retained_fail_plus_missing_evidence_is_not_a_quality_rejection():
    r = fr(reviewer_verdict="FAIL", api_failure="reviewer_evidence_missing",
           executor_success=True, attempts=2)
    assert (r.review_execution, r.reviewer_verdict) == (ReviewExecution.failed_to_complete, None)
    assert r.failure_category == FailureCategory.internal_pipeline_failure.value
    assert r.outcome_code != FailureCategory.review_rejected.value


def test_the_closing_message_for_that_run_blames_us_not_the_geometry():
    msg = render_message(fr(reviewer_verdict="FAIL", api_failure="reviewer_evidence_missing"))
    assert "did not pass review" not in msg
    assert "on our side" in msg


# a genuine rejection survives
def test_an_eligible_rejection_is_still_reported_as_a_rejection():
    r = fr(reviewer_verdict="FAIL", executor_success=True)
    assert (r.review_execution, r.reviewer_verdict) == (ReviewExecution.completed, ReviewVerdict.failed)
    assert r.failure_category == FailureCategory.review_rejected.value
    assert "did not pass review" in render_message(r)


def test_an_eligible_rejection_is_not_downgraded_by_an_unrelated_attempt_count():
    for attempts in (1, 2, 5):
        r = fr(reviewer_verdict="FAIL", attempts=attempts)
        assert (r.review_execution, r.reviewer_verdict) == (ReviewExecution.completed, ReviewVerdict.failed), attempts


# every reviewer-side failure marker
@pytest.mark.parametrize("marker", REVIEWER_MARKERS)
def test_every_reviewer_side_marker_is_inconclusive_never_rejected(marker):
    r = fr(api_failure=marker)
    assert (r.review_execution, r.reviewer_verdict) == (ReviewExecution.failed_to_complete, None), marker
    assert r.failure_category != FailureCategory.review_rejected.value, marker


@pytest.mark.parametrize("marker", REVIEWER_MARKERS)
def test_a_reviewer_marker_outranks_a_retained_verdict_of_either_polarity(marker):
    for verdict in ("PASS", "FAIL"):
        r = fr(api_failure=marker, reviewer_verdict=verdict)
        assert (r.review_execution, r.reviewer_verdict) == (ReviewExecution.failed_to_complete, None), (marker, verdict)


def test_an_internal_reviewer_failure_with_no_prior_verdict_is_still_inconclusive():
    r = fr(api_failure="reviewer_exhausted", reviewer_verdict="")
    assert (r.review_execution, r.reviewer_verdict) == (ReviewExecution.failed_to_complete, None)
    assert r.review_execution is not ReviewExecution.not_reached


# the reviewer not reached
def test_a_gate_failure_means_the_reviewer_was_not_reached():
    r = fr(executor_success=False, failed_gate="patch_contract", reviewer_verdict="")
    assert (r.review_execution, r.reviewer_verdict) == (ReviewExecution.not_reached, None)
    assert r.failure_category == FailureCategory.gate_failed.value


def test_a_gate_failure_does_not_inherit_an_earlier_attempts_verdict():
    for verdict in ("PASS", "FAIL"):
        r = fr(failed_gate="mesh_quality", reviewer_verdict=verdict)
        assert (r.review_execution, r.reviewer_verdict) == (ReviewExecution.not_reached, None), verdict
        assert r.failure_category == FailureCategory.gate_failed.value, verdict


def test_a_non_reviewer_failure_with_no_verdict_is_not_claimed_as_a_review():
    r = fr(executor_success=False, api_failure="api_error")
    assert (r.review_execution, r.reviewer_verdict) == (ReviewExecution.not_reached, None)
    assert r.failure_category == FailureCategory.internal_pipeline_failure.value


def test_an_executor_that_never_validated_a_mesh_has_no_review_to_report():
    r = fr(executor_success=False, reviewer_verdict="FAIL")
    assert (r.review_execution, r.reviewer_verdict) == (ReviewExecution.not_reached, None)


# precedence order
def test_a_technical_gate_failure_outranks_a_retained_review_verdict():
    assert fr(failed_gate="solvability", reviewer_verdict="FAIL").failure_category == \
        FailureCategory.gate_failed.value


def test_a_top_level_timeout_outranks_an_inconclusive_review():
    r = fr(api_failure="reviewer_exhausted", pipeline_timed_out=True)
    assert (r.review_execution, r.reviewer_verdict) == (ReviewExecution.failed_to_complete, None)
    assert r.failure_category == FailureCategory.timed_out.value


def test_a_top_level_timeout_never_outranks_an_eligible_rejection():
    r = fr(reviewer_verdict="FAIL", pipeline_timed_out=True)
    assert r.failure_category == FailureCategory.review_rejected.value


def test_delivery_failure_still_reported_when_the_review_actually_passed():
    r = fr(reviewer_verdict="PASS", executor_success=True, required_ready=False)
    assert (r.review_execution, r.reviewer_verdict) == (ReviewExecution.completed, ReviewVerdict.passed)
    assert r.failure_category == FailureCategory.delivery_failed.value


def test_attempts_exhausted_still_reported_when_the_reviewer_was_never_reached():
    r = fr(executor_success=False, attempts=5, attempts_max=5)
    assert r.failure_category == FailureCategory.attempts_exhausted.value


# success is not disturbed
def test_a_successful_run_is_unaffected():
    r = fr(status=TerminalStatus.succeeded, reviewer_verdict="PASS", required_ready=True,
           delivered_types=["mesh_bundle"])
    assert (r.review_execution, r.reviewer_verdict) == (ReviewExecution.completed, ReviewVerdict.passed)
    assert r.outcome_code == "success"
    assert r.failure_category is None
    assert "Review: passed" in render_message(r)


def test_a_failing_review_can_never_produce_a_success():
    for marker in REVIEWER_MARKERS:
        r = fr(api_failure=marker, reviewer_verdict="PASS")
        assert r.status is TerminalStatus.failed
        assert r.outcome_code != "success"
        assert r.required_ready is False


# the invariant itself, exhaustively
CONCLUDED = lambda ex, fg, af, vd: bool(ex) and not fg and not af and vd in ("PASS", "FAIL")  # noqa: E731


def test_review_rejected_is_reachable_only_from_a_concluded_review():
    violations = []
    for ex, fg, af, vd in itertools.product(
            (True, False),
            ("", "mesh_quality", "patch_contract", "solvability"),
            ("", *REVIEWER_MARKERS, "api_error", "provider_timeout"),
            ("", "PASS", "FAIL", "fail", " FAIL ")):
        r = fr(executor_success=ex, failed_gate=fg, api_failure=af, reviewer_verdict=vd)
        want = CONCLUDED(ex, fg, af, vd.strip().upper()) and vd.strip().upper() == "FAIL"
        if ((r.review_execution, r.reviewer_verdict) == (ReviewExecution.completed, ReviewVerdict.failed)) != want:
            violations.append(("review", ex, fg, af, vd, (r.reviewer_verdict.value if r.reviewer_verdict else None)))
        if (r.outcome_code == FailureCategory.review_rejected.value) != want:
            violations.append(("outcome", ex, fg, af, vd, r.outcome_code))
    assert not violations, violations


def test_no_failure_ever_renders_a_rejection_headline_without_a_concluded_review():
    for af in REVIEWER_MARKERS:
        for vd in ("", "PASS", "FAIL"):
            assert "did not pass review" not in render_message(
                fr(api_failure=af, reviewer_verdict=vd)), (af, vd)


# round-trips and surfaces
def test_the_inconclusive_state_round_trips_through_the_durable_record():
    from meshpipeline.application.final_result import FinalResult
    r = fr(api_failure="reviewer_evidence_missing", reviewer_verdict="FAIL")
    back = FinalResult.from_dict(r.to_dict())
    assert (back.review_execution, back.reviewer_verdict) == (ReviewExecution.failed_to_complete, None)
    assert back.outcome_code == r.outcome_code
    assert back.failure_category == r.failure_category


def test_the_verdict_execution_invariant_holds_in_both_directions():
    for kw in ({}, {"reviewer_verdict": "FAIL"}, {"reviewer_verdict": "PASS"},
               {"api_failure": "reviewer_exhausted"},
               {"api_failure": "reviewer_exhausted", "reviewer_verdict": "FAIL"},
               {"failed_gate": "mesh_quality", "reviewer_verdict": "PASS"},
               {"executor_success": False}):
        r = fr(**kw)
        assert (r.reviewer_verdict is not None) == (
            r.review_execution is ReviewExecution.completed), kw


def test_a_final_result_violating_the_invariant_cannot_be_constructed():
    from meshpipeline.application.final_result import FinalResult
    with pytest.raises(ValueError):
        FinalResult(schema_version=4, job_id="j", owner_id="o", status=TerminalStatus.failed,
                    reviewer_verdict=ReviewVerdict.failed,
                    review_execution=ReviewExecution.failed_to_complete)
    with pytest.raises(ValueError):
        FinalResult(schema_version=4, job_id="j", owner_id="o", status=TerminalStatus.failed,
                    reviewer_verdict=None, review_execution=ReviewExecution.completed)
