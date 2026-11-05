# Responsibility: Verify cancellation is a terminal status of its own - written by the owner's authority, never derived by a worker, never a failure.
from __future__ import annotations

from meshpipeline.application.final_result import (
    CANCELLED_MESSAGE,
    FailureCategory,
    FinalResult,
    RunOutcome,
    TerminalStatus,
    build_cancelled_result,
    derive_terminal_status,
    render_message,
)
from meshpipeline.persistence.job_state import TERMINAL_STATES
from meshpipeline.persistence.models import JobStatus


class _Log:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


# the vocabulary

def test_cancelled_is_a_job_status_and_it_is_terminal():
    assert JobStatus.cancelled.value == "cancelled"
    assert {s.value for s in JobStatus} == {
        "pending", "running", "succeeded", "failed", "queued", "pending_review", "cancelled"}
    assert JobStatus.cancelled in TERMINAL_STATES


def test_a_cancel_is_not_a_failure_category():
    # nothing broke: the taxonomy of what went wrong carries no entry for the owner's decision
    assert "cancelled" not in {c.value for c in FailureCategory}


def test_terminal_status_names_all_three_endings():
    assert {s.value for s in TerminalStatus} == {"succeeded", "failed", "cancelled"}


def test_timed_out_category_survives_for_the_pipeline_deadline():
    assert FailureCategory.timed_out.value == "timed_out"


# the worker never derives it

def test_the_worker_never_derives_cancelled():
    # every answer the run's own outcome can produce is succeeded or failed; `cancelled` has one
    # writer and it is not this function
    outcomes = [RunOutcome(),
                RunOutcome(reviewer_verdict="PASS", executor_success=True),
                RunOutcome(reviewer_verdict="FAIL", executor_success=True),
                RunOutcome(reviewer_verdict="", executor_success=False, retry_count=3)]
    for outcome in outcomes:
        decision = derive_terminal_status(outcome, job_id="j", jlog=_Log())
        assert decision.status in (JobStatus.succeeded, JobStatus.failed)


def test_the_cancel_authority_is_the_only_writer_of_the_status():
    import subprocess

    # every `JobStatus.cancelled` in production code is either the vocabulary, the transition
    # table, a read (a filter or a comparison), or the ONE write in the cancel authority
    proc = subprocess.run(
        ["git", "grep", "-n", "JobStatus.cancelled", "--", "src"], capture_output=True, text=True)
    assert proc.returncode in (0, 1), proc.stderr
    writers = [line for line in proc.stdout.splitlines()
               if "transition(" in line and "JobStatus.cancelled" in line]
    assert writers and all("application/job_cancel.py" in w for w in writers), writers


# the record

def test_a_cancelled_record_delivers_nothing_and_claims_no_verdict():
    fr = build_cancelled_result(job_id="j", owner_id="o", engine="cfmesh", attempts=2)
    assert fr.status is TerminalStatus.cancelled
    assert fr.failure_category is None and fr.outcome_code == "cancelled"
    assert fr.executor_success is False and fr.required_ready is False
    assert fr.delivered_types == [] and fr.reviewer_verdict is None
    assert fr.missing_outputs, "a known engine's required deliverables are reported missing"
    assert fr.attempts == 2


def test_a_cancelled_record_round_trips_and_renders_the_plain_line():
    fr = build_cancelled_result(job_id="j", owner_id="o", engine="cfmesh")
    again = FinalResult.from_dict(fr.to_dict())
    assert again.status is TerminalStatus.cancelled
    text = render_message(again)
    assert text.splitlines()[0] == CANCELLED_MESSAGE
    assert "did not complete successfully" not in text
    assert "try running" not in text.lower(), "a cancel is not something to retry"


def test_an_unknown_engine_still_builds_a_cancelled_record():
    fr = build_cancelled_result(job_id="j", owner_id="o", engine="")
    assert fr.missing_outputs == [] and fr.attempts == 0
    assert render_message(fr).startswith(CANCELLED_MESSAGE)
