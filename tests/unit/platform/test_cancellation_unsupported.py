# Responsibility: Verify cancellation is not a job status or failure category, and terminal means succeeded or failed.
from __future__ import annotations

from meshpipeline.application.final_result import FailureCategory, TerminalStatus
from meshpipeline.persistence.models import JobStatus


def test_no_cancelled_job_status():
    assert not hasattr(JobStatus, "cancelled")
    assert {s.value for s in JobStatus} == {
        "pending", "running", "succeeded", "failed", "queued", "pending_review"}


def test_no_cancelled_failure_category():
    assert "cancelled" not in {c.value for c in FailureCategory}


def test_terminal_status_is_only_succeeded_or_failed():
    assert {s.value for s in TerminalStatus} == {"succeeded", "failed"}


def test_timed_out_category_survives_for_the_pipeline_deadline():
    # 's top-level deadline uses timed_out - it stays reachable, unlike cancelled.
    assert FailureCategory.timed_out.value == "timed_out"
