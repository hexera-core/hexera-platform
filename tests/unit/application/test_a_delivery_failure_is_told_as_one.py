# Responsibility: Prove a run that failed at the HANDOVER is told as a delivery failure - category, outcome code, account and next step - whatever its review did.
# Boundaries: the status decision, the final result and its rendered message; no run, no store, no database.
# The live failure (Corintis cold plate, job 0ff6f9e5, 2026-11): 283,410,955 cells meshed in 5h07m
# on 64 cores, every gate passed, the conjugate interface conformal on both sides.
# derive_terminal_status FORGAVE the inconclusive review and granted succeeded - which is the only
# reason the mesh was ever handed over - and then delivery failed, because the 10,616,027,998-byte
# OpenFOAM case bundle would not fit artifacts.size_bytes while that column was int4.
# The record read failure_cause "review_incomplete", outcome_code "internal_pipeline_failure", and
# "You can run it again ... with the same request - nothing needs to change." The reviewer was
# blamed for a 32-bit column, and the user was sent back into a five-hour mesh that would have
# stopped in exactly the same place.
from __future__ import annotations

import pytest

from meshpipeline.application.final_result import (
    REVIEW_INCONCLUSIVE,
    FailureCategory,
    ReviewExecution,
    ReviewVerdict,
    RunOutcome,
    TerminalStatus,
    apply_delivery,
    build_final_result,
    derive_terminal_status,
    render_message,
)

MARKER = "reviewer_evidence_missing"
CAVEAT = {"kind": REVIEW_INCONCLUSIVE, "marker": MARKER, "reruns": 0}


class _Log:
    def __init__(self):
        self.lines = []

    def info(self, *a, **k):
        self.lines.append(a)

    warning = info
    error = info


def _plate(**over):
    """Job 0ff6f9e5's terminal facts: a validated mesh, no failed gate, a review that did not
    conclude and was forgiven, and a handover that failed afterwards."""
    base = {"job_id": "0ff6f9e5", "owner_id": "rehaan-dev", "status": TerminalStatus.failed,
            "engine": "cfmesh_conjugate", "purpose": "conjugate_heat_transfer",
            "dimensionality": "3D", "approved_snapshot_id": "snap-0ff6",
            "executor_success": True, "reviewer_verdict": "", "failed_gate": "",
            "api_failure": MARKER, "attempts": 1, "attempts_max": 3,
            "required_ready": False, "delivered_types": [], "optional_warnings": [],
            "requirement_caveats": [dict(CAVEAT)], "delivery_failed": True}
    base.update(over)
    return build_final_result(**base)


def _plate_state(**over):
    # the terminal graph state of job 0ff6f9e5 as the run left it
    state = {"api_failure": f"<<API_FAILURE:{MARKER}>>", "executor_success": True,
             "retry_count": 1, "executor_failed_gate": "", "engine": "cfmesh_conjugate",
             "purpose": "conjugate_heat_transfer", "reviewer_verdict": "",
             "review_rerun_count": 0}
    state.update(over)
    return state


def _delivery(ok: bool):
    """The uploader's own outcome - the real one, so a change to its shape is caught here."""
    from meshpipeline.application.artifact_uploader import RunDelivery
    from meshpipeline.persistence.models import FailedReason
    if ok:
        return RunDelivery(["mesh_bundle", "viewer_data"], True, None, None)
    # the bind the plate's 10,616,027,998-byte bundle died on, classified as the uploader
    # classifies it (errors.classify_exception -> a system class -> a FailedReason)
    return RunDelivery([], False, FailedReason.unhandled,
                       ValueError("invalid input for query argument $8: 10616027998 "
                                  "(value out of int32 range)"))


# what the record says the run failed on


def test_the_cold_plate_failed_on_delivery_not_on_its_review():
    r = _plate()
    # the exact shape of 0ff6f9e5: no verdict for this attempt, and the review did not conclude
    assert (r.review_execution, r.reviewer_verdict) == (ReviewExecution.failed_to_complete, None)
    assert r.failure_category == FailureCategory.delivery_failed.value
    assert r.outcome_code == FailureCategory.delivery_failed.value
    assert r.failure_cause == "delivery_failed"
    assert FailureCategory.internal_pipeline_failure.value not in (r.outcome_code,
                                                                   r.failure_category)
    # both required classes were rolled back with the transaction that failed on size_bytes
    assert r.missing_outputs == ["mesh_bundle", "viewer_data"]


def test_the_account_names_the_store_and_the_message_never_reads_as_a_generic_internal_failure():
    r = _plate()
    assert "could not be stored for download" in r.failure_detail
    assert "our fault" in r.failure_detail
    msg = render_message(r)
    assert "Something went wrong on our side while running the job." not in msg
    assert "could not be stored for download" in msg
    assert "did not pass review" not in msg


def test_the_next_step_does_not_send_the_user_back_into_the_same_five_hour_refusal():
    r = _plate()
    nxt = r.failure_next_step
    assert nxt, "a delivery failure that says nothing about what to do next says nothing at all"
    # the sentence job 0ff6f9e5 actually shipped, which was wrong twice over
    assert "nothing needs to change" not in nxt
    assert "with the same request" not in nxt
    assert "rebuilds the whole mesh" in nxt and "same storing step" in nxt
    assert "not worth starting" in nxt
    assert nxt in render_message(r), "the next step must reach the user, not just the record"


def test_the_inconclusive_review_survives_as_a_caveat_instead_of_becoming_the_cause():
    r = _plate()
    assert dict(CAVEAT) in r.requirement_caveats, (
        "the only durable trace that no verdict was reached on this mesh")
    assert "no review verdict was reached" in r.failure_detail
    assert "not what failed this run" in r.failure_detail
    assert r.failure_cause != "review_incomplete"


def test_a_passed_review_whose_delivery_failed_reads_the_same_way():
    r = _plate(reviewer_verdict="PASS", api_failure="", requirement_caveats=[])
    assert (r.review_execution, r.reviewer_verdict) == (ReviewExecution.completed,
                                                        ReviewVerdict.passed)
    assert r.failure_category == FailureCategory.delivery_failed.value
    assert "built and reviewed" in r.failure_detail
    assert "no review verdict was reached" not in r.failure_detail


# the whole path, from the forgiven review to the record


def test_the_run_is_succeeded_when_delivery_begins_and_a_delivery_failure_names_itself():
    from meshpipeline.application.terminal_finalize import (
        TerminalAssembly,
        build_terminal_result,
    )
    from meshpipeline.persistence.models import JobStatus

    outcome = RunOutcome.from_graph_state(_plate_state())
    decision = derive_terminal_status(outcome, job_id="0ff6f9e5", jlog=_Log())
    # THE FORGIVENESS, untouched: this is the only reason the mesh was handed over at all
    assert decision.status == JobStatus.succeeded and decision.failed_reason is None
    assert decision.delivery_failed is False

    downgraded = apply_delivery(decision, _delivery(False))
    assert downgraded.status == JobStatus.failed
    assert downgraded.delivery_failed is True

    out = build_terminal_result(TerminalAssembly(
        job_id="0ff6f9e5", owner_id="rehaan-dev", status=downgraded.status,
        failed_reason=downgraded.failed_reason, engine="cfmesh_conjugate",
        purpose="conjugate_heat_transfer", dimensionality="3D", executor_success=True,
        api_failure=MARKER, attempts=1, attempts_max=3,
        requirement_caveats=[dict(CAVEAT)],
        delivery_failed=downgraded.delivery_failed), delivered_types=[])
    assert out.result.failure_category == FailureCategory.delivery_failed.value
    assert out.closing_message == render_message(out.result)
    assert "could not be stored for download" in out.closing_message
    assert "nothing needs to change" not in out.closing_message


# what must not change


def test_an_inconclusive_review_whose_delivery_succeeded_still_reports_succeeded():
    from meshpipeline.persistence.models import JobStatus

    outcome = RunOutcome.from_graph_state(_plate_state())
    decision = derive_terminal_status(outcome, job_id="0ff6f9e5", jlog=_Log())
    after = apply_delivery(decision, _delivery(True))
    assert after.status == JobStatus.succeeded and after.failed_reason is None
    assert after.delivery_failed is False

    r = _plate(status=TerminalStatus.succeeded, required_ready=True,
               delivered_types=["mesh_bundle", "viewer_data"], delivery_failed=False)
    assert r.outcome_code == "success" and r.failure_category is None
    assert r.review_execution is ReviewExecution.failed_to_complete
    assert "Required deliverable: ready to download" in render_message(r)


def test_a_review_that_did_not_finish_with_no_delivery_is_still_told_as_the_review():
    # job 53bbce4b's shape, where nothing was ever handed over: rule 1 still owns it
    r = _plate(delivery_failed=False, requirement_caveats=[])
    assert r.failure_category == FailureCategory.internal_pipeline_failure.value
    assert r.failure_cause == "review_incomplete"
    assert "did not finish" in r.failure_detail


@pytest.mark.parametrize("over,expected", [
    ({"failed_gate": "manifest_valid"}, FailureCategory.gate_failed),
    ({"failed_gate": "geometry"}, FailureCategory.input_rejected),
    ({"attempts": 3}, FailureCategory.attempts_exhausted),
])
def test_a_run_that_never_reached_the_handover_is_untouched_by_the_handover_rule(over, expected):
    # the new rule is keyed on the delivery's own fact and on nothing else: `required_ready` is
    # False on every one of these too, and none of them delivered anything
    r = _plate(executor_success=False, api_failure="", delivery_failed=False,
               requirement_caveats=[], **over)
    assert r.failure_category == expected.value
