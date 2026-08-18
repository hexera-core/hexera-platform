# Responsibility: Verify the terminal status of each outcome combination, where a system failure never becomes success.
from __future__ import annotations

import asyncio

import pytest

from meshpipeline.application.artifact_uploader import RunDelivery
from meshpipeline.application.final_result import (
    RunOutcome,
    StatusDecision,
    apply_delivery,
    derive_terminal_status,
)
from meshpipeline.persistence.models import FailedReason, JobStatus


class _Log:
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    import meshpipeline.errors as errs
    seen: list = []
    monkeypatch.setattr(errs, "record_dead_letter", lambda *a, **k: seen.append(a))
    return seen


def _derive(**kw) -> StatusDecision:
    return derive_terminal_status(RunOutcome(**kw), job_id="job-1", jlog=_Log())


# the outcome table

#: (api_failure, verdict, executor_success) -> (status, reason)
TABLE = [
    ("",            "PASS", True,  JobStatus.succeeded, None),
    ("",            "PASS", False, JobStatus.failed,    FailedReason.mesh_generation),
    ("",            "FAIL", True,  JobStatus.failed,    FailedReason.reviewer_rejected),
    ("",            "FAIL", False, JobStatus.failed,    FailedReason.mesh_generation),
    ("",            "",     True,  JobStatus.failed,    FailedReason.reviewer_rejected),
    ("",            "",     False, JobStatus.failed,    FailedReason.mesh_generation),
    ("openai: 500", "PASS", True,  JobStatus.failed,    None),   # reason classified, never success
    ("minio: down", "FAIL", False, JobStatus.failed,    None),
]


@pytest.mark.parametrize("api,verdict,ok,status,reason", TABLE)
def test_every_outcome_combination(api, verdict, ok, status, reason):
    d = _derive(api_failure=api, reviewer_verdict=verdict, executor_success=ok)
    assert d.status is status
    if reason is not None:
        assert d.failed_reason is reason
    if status is JobStatus.succeeded:
        assert d.failed_reason is None and d.succeeded
    else:
        assert not d.succeeded


def test_a_system_failure_never_becomes_success_however_good_the_review():
    d = _derive(api_failure="openai: timeout", reviewer_verdict="PASS", executor_success=True)
    assert d.status is JobStatus.failed
    assert d.failed_reason is not FailedReason.reviewer_rejected, (
        "a dependency outage was recorded as though the reviewer rejected the mesh")


def test_a_system_failure_is_recorded_as_a_dead_letter(_quiet):
    _derive(api_failure="minio: unreachable")
    assert _quiet, "a system failure produced no inspectable dead-letter record"


def test_an_unvalidated_mesh_cannot_ship_on_a_pass_alone():
    d = _derive(reviewer_verdict="PASS", executor_success=False)
    assert d.status is JobStatus.failed and d.failed_reason is FailedReason.mesh_generation


def test_an_incomplete_report_is_not_success():
    assert _derive().status is JobStatus.failed


def test_the_marker_wrapped_api_failure_is_unwrapped():
    o = RunOutcome.from_graph_state({"api_failure": "<<API_FAILURE:openai: 429>>"})
    assert o.api_failure == "openai: 429"


def test_a_bare_api_failure_survives_unchanged():
    assert RunOutcome.from_graph_state({"api_failure": "minio: down"}).api_failure == "minio: down"


def test_from_graph_state_defaults_are_not_success():
    o = RunOutcome.from_graph_state({})
    assert _derive(**o.__dict__).status is JobStatus.failed


# delivery downgrade

def test_delivery_failure_downgrades_a_success():
    ok = _derive(reviewer_verdict="PASS", executor_success=True)
    assert ok.status is JobStatus.succeeded
    down = apply_delivery(ok, RunDelivery([], False, FailedReason.unhandled, RuntimeError("store")))
    assert down.status is JobStatus.failed and down.failed_reason is FailedReason.unhandled


def test_a_delivered_success_stays_succeeded():
    ok = _derive(reviewer_verdict="PASS", executor_success=True)
    assert apply_delivery(ok, RunDelivery([{"k": 1}], True, None, None)).status is JobStatus.succeeded


def test_delivery_does_not_resurrect_a_failure():
    bad = _derive(reviewer_verdict="FAIL", executor_success=True)
    assert apply_delivery(bad, RunDelivery([{"k": 1}], True, None, None)).status is JobStatus.failed


def test_no_delivery_leaves_the_decision_untouched():
    bad = _derive(reviewer_verdict="FAIL", executor_success=False)
    assert apply_delivery(bad, None) == bad


# cancellation

async def test_cancellation_is_not_a_derived_status_at_all():
    assert issubclass(asyncio.CancelledError, BaseException)
    assert not issubclass(asyncio.CancelledError, Exception), (
        "CancelledError became an Exception - the run's `except Exception` would swallow a "
        "cancellation and derive a generic failure for it")

    async def _cancelled():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        try:
            await _cancelled()
        except Exception:                     # noqa: BLE001 - exactly the run's handler shape
            pytest.fail("a cancellation was caught by `except Exception`")


def test_there_is_no_cancelled_terminal_status():
    from meshpipeline.application.final_result import FailureCategory, TerminalStatus

    assert {s.value for s in TerminalStatus} == {"succeeded", "failed"}
    assert "cancelled" not in {c.value for c in FailureCategory}


# mutation guard

def test_the_orchestrator_no_longer_derives_the_status_itself():
    import inspect

    import meshpipeline.application.pipeline_run as pr

    src = inspect.getsource(pr)
    assert "classify_api_failure" not in src, "pipeline_run classifies API failures again"
    assert "FailedReason.reviewer_rejected" not in src, "pipeline_run picks the failure reason again"
    assert "derive_terminal_status" in src and "apply_delivery" in src
