# Responsibility: Prove a review that did not finish delivers a validated mesh with that stated, and is otherwise told in plain words - never as a bare "something went wrong".
# Boundaries: the final result, its status, its rendered message and the terminal closing; no run, no database.
# The live failure (Windsor body, job 53bbce4b, 2026-09-30): the mesh passed every gate and a trial
# solve, the review stalled, and the user read "Something went wrong on our side while running the
# job." with failure_cause and failed_gate both empty. The chat's closing said a third thing.
# A good mesh is never an internal failure: where it may be delivered it is, with the review's
# absence stated (the tests at the end); where it may not (a dispute, a failed gate), the failure
# names the review that did not finish (the tests here).
from __future__ import annotations

import pytest

from meshpipeline.application.final_result import (
    FailureCategory,
    FinalResult,
    ReviewExecution,
    TerminalStatus,
    build_final_result,
    render_message,
)
from meshpipeline.errors import FailureClass, classify_api_failure

_GENERIC = "Something went wrong on our side while running the job."


def _fr(api_failure="reviewer_stalled", *, executor_success=True, reruns=0, verdict=""):
    return build_final_result(
        job_id="53bbce4b", owner_id="o", status=TerminalStatus.failed, engine="snappy",
        purpose="external_cfd", dimensionality="3D", approved_snapshot_id="",
        executor_success=executor_success, reviewer_verdict=verdict, failed_gate="",
        api_failure=api_failure, attempts=1, attempts_max=3, required_ready=False,
        delivered_types=[], optional_warnings=[], review_reruns=reruns)


def test_an_undeliverable_outcome_names_the_review_not_something():
    fr = _fr("reviewer_stalled", reruns=2)
    msg = render_message(fr)
    assert fr.review_execution is ReviewExecution.failed_to_complete
    assert fr.outcome_code == FailureCategory.internal_pipeline_failure.value
    assert fr.failure_cause == "review_incomplete"
    assert _GENERIC not in msg
    assert "passed every automatic check" in msg, "the mesh was validated - say so"
    assert "stopped making progress" in msg
    assert "twice" in msg, "the reruns already spent are part of the account"
    assert "on our side" in msg and "run it again" in msg
    assert "did not pass review" not in msg, "a review that did not conclude judged nothing"


@pytest.mark.parametrize("marker,words", [
    ("reviewer_stalled", "stopped making progress"),
    ("reviewer_exhausted", "used up its time"),
    ("reviewer_render_unavailable", "draws the mesh"),
    ("reviewer_evidence_missing", "everything it needs"),
    ("reviewer_timeout", "was not available"),
    ("<<API_FAILURE:reviewer_server_error>>", "was not available"),
])
def test_each_way_a_review_stops_is_said_in_its_own_words(marker, words):
    fr = _fr(marker)
    assert fr.failure_cause == "review_incomplete"
    assert words in fr.failure_detail


def test_an_unvalidated_mesh_is_never_called_checked():
    fr = _fr("reviewer_evidence_missing", executor_success=False)
    assert "passed every automatic check" not in fr.failure_detail
    assert "could not run" in fr.failure_detail


def test_a_builder_failure_beside_a_retained_verdict_is_not_blamed_on_the_review():
    # failed_to_complete also covers a verdict RETAINED from an earlier attempt next to another
    # node's provider failure - there the review never ran, so it must not be named
    fr = _fr("<<API_FAILURE:builder_timeout>>", verdict="FAIL")
    assert fr.review_execution is ReviewExecution.failed_to_complete
    assert fr.failure_cause == "" and fr.failure_detail == ""


def test_the_account_survives_the_durable_round_trip():
    fr = _fr("reviewer_stalled", reruns=1)
    back = FinalResult.from_dict(fr.to_dict())
    assert render_message(back) == render_message(fr)
    assert "once" in render_message(back)


def test_the_chat_closing_and_the_job_page_say_the_same_thing():
    from meshpipeline.application.terminal_finalize import (
        TerminalAssembly,
        build_terminal_result,
    )
    from meshpipeline.persistence.models import FailedReason, JobStatus
    out = build_terminal_result(TerminalAssembly(
        job_id="53bbce4b", owner_id="o", status=JobStatus.failed,
        failed_reason=FailedReason.api_failure, engine="snappy", purpose="external_cfd",
        dimensionality="3D", executor_success=True, api_failure="reviewer_stalled",
        attempts=1, attempts_max=3, review_reruns=2,
        pre_composed_message="The review did not produce findings for all required quality "
                             "criteria, so we could not verify your mesh."),
        delivered_types=[])
    assert out.closing_message == render_message(out.result)
    assert "stopped making progress" in out.closing_message


def test_a_stall_is_ours_and_is_not_a_provider_outage():
    fc = classify_api_failure("reviewer_stalled")
    assert fc is FailureClass.REVIEW_EVIDENCE_MISSING and fc.is_system
    assert not fc.is_retryable, "a stall is rerun by the graph's own rule, not as a brownout"


# a good mesh is never an internal failure: delivered, with the review's absence stated

class _Log:
    def __init__(self):
        self.lines = []

    def info(self, *a, **k):
        self.lines.append(a)

    warning = info


def _windsor_state(**over):
    # the terminal graph state of job 53bbce4b, after two reruns that also stalled
    state = {"api_failure": "reviewer_stalled", "executor_success": True, "retry_count": 1,
             "executor_failed_gate": "", "engine": "snappy", "flow_topology": "external",
             "review_rerun_count": 2, "reviewer_verdict": ""}
    state.update(over)
    return state


def test_the_windsor_mesh_is_delivered_with_the_review_stated():
    from meshpipeline.application.final_result import (
        REVIEW_INCONCLUSIVE,
        RunOutcome,
        derive_terminal_status,
        review_inconclusive_caveat,
    )
    from meshpipeline.persistence.models import JobStatus
    outcome = RunOutcome.from_graph_state(_windsor_state())
    decision = derive_terminal_status(outcome, job_id="53bbce4b", jlog=_Log())
    assert decision.status == JobStatus.succeeded and decision.failed_reason is None
    caveat = review_inconclusive_caveat(outcome)
    assert caveat == {"kind": REVIEW_INCONCLUSIVE, "marker": "reviewer_stalled", "reruns": 2}

    fr = build_final_result(
        job_id="53bbce4b", owner_id="o", status=TerminalStatus.succeeded, engine="snappy",
        purpose="external_cfd", dimensionality="3D", approved_snapshot_id="",
        executor_success=True, reviewer_verdict="", failed_gate="",
        api_failure="reviewer_stalled", attempts=1, attempts_max=3, required_ready=True,
        delivered_types=["mesh_bundle", "viewer_data"], optional_warnings=[],
        requirement_caveats=[caveat], review_reruns=2)
    msg = render_message(fr)
    assert fr.outcome_code == "success" and fr.reviewer_verdict is None
    assert fr.review_execution is ReviewExecution.failed_to_complete
    assert msg.startswith("Delivered with a stated caveat:")
    assert "did not finish (it was started again twice)" in msg
    assert "stopped making progress" in msg and "on our side" in msg
    assert "Every automatic check on the mesh passed" in msg
    assert "Review: did not finish" in msg and "Review: passed" not in msg
    assert _GENERIC not in msg
    assert "Required deliverable: ready to download" in msg


@pytest.mark.parametrize("over", [
    {"executor_success": False},                       # nothing validated - nothing to deliver
    {"executor_failed_gate": "quality_floor"},          # a hard check failed
    {"user_dispute": {"flags": [{"x": 0, "y": 0, "z": 0}]}},  # flags need a judgement
    {"api_failure": "<<API_FAILURE:builder_timeout>>"},  # not the review's own marker
])
def test_only_a_validated_mesh_whose_review_alone_stopped_is_delivered(over):
    from meshpipeline.application.final_result import (
        RunOutcome,
        derive_terminal_status,
        review_inconclusive_caveat,
    )
    from meshpipeline.persistence.models import JobStatus
    outcome = RunOutcome.from_graph_state(_windsor_state(**over))
    assert review_inconclusive_caveat(outcome) is None
    assert derive_terminal_status(outcome, job_id="j", jlog=_Log()).status == JobStatus.failed


def test_a_delivery_without_a_verdict_must_say_so():
    with pytest.raises(ValueError, match="review_inconclusive"):
        build_final_result(
            job_id="j", owner_id="o", status=TerminalStatus.succeeded, engine="snappy",
            purpose="external_cfd", dimensionality="3D", approved_snapshot_id="",
            executor_success=True, reviewer_verdict="", failed_gate="",
            api_failure="reviewer_stalled", attempts=1, attempts_max=3, required_ready=True,
            delivered_types=["mesh_bundle"], optional_warnings=[])


def test_a_failure_note_never_closes_a_delivered_run():
    from meshpipeline.application.terminal_finalize import (
        TerminalAssembly,
        build_terminal_result,
    )
    from meshpipeline.persistence.models import JobStatus
    out = build_terminal_result(TerminalAssembly(
        job_id="53bbce4b", owner_id="o", status=JobStatus.succeeded, failed_reason=None,
        engine="snappy", purpose="external_cfd", dimensionality="3D", executor_success=True,
        api_failure="reviewer_stalled", attempts=1, attempts_max=3, review_reruns=1,
        requirement_caveats=[{"kind": "review_inconclusive", "marker": "reviewer_stalled",
                              "reruns": 1}],
        pre_composed_message="The review did not produce findings for all required quality "
                             "criteria, so we could not verify your mesh. Please try again."),
        delivered_types=["mesh_bundle", "viewer_data"])
    assert out.result.status is TerminalStatus.succeeded
    assert out.closing_message == render_message(out.result)
    assert "could not verify your mesh" not in out.closing_message
    assert "started again once" in out.closing_message
