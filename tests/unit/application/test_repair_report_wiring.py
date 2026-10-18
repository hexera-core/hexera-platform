# Responsibility: Verify the orchestrator delivers the inspection report on every terminal outcome, not only on success.
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import meshpipeline.application.pipeline_run as wt

SOURCE = Path(inspect.getfile(wt)).read_text()


def _finalize_body() -> str:
    """The orchestration from the terminal decision down to the status commit."""
    start = SOURCE.index("_uploaded_artifacts: list[dict] = []")
    end = SOURCE.index("from meshpipeline.application.terminal_finalize import", start)
    return SOURCE[start:end]


def test_the_report_is_delivered_outside_the_success_branch():
    body = _finalize_body()
    call_at = body.index("deliver_repair_report(")
    success_at = body.index("if final_status == JobStatus.succeeded:")
    # BEFORE the success branch, and therefore on a blocked, failed or timed-out run too - which
    # is the whole reason this delivery is separate from artifact_uploader's.
    assert call_at < success_at, (
        "the inspection report must be delivered on every terminal outcome: the readers who need "
        "it most are on runs that produced no mesh")


def test_it_is_guarded_only_by_whether_an_inspection_happened():
    body = _finalize_body()
    guard = body[:body.index("deliver_repair_report(")]
    assert 'if final_state.get("repair_report"):' in guard
    # not keyed to a status, a verdict or a workspace
    for wrong in ("executor_success", "reviewer_verdict", "openfoam_workspace"):
        assert wrong not in guard.rsplit('if final_state.get("repair_report"):', 1)[-1]


def test_the_report_carries_the_runs_currency_and_the_fence():
    body = _finalize_body()
    call = body[body.index("deliver_repair_report("):]
    call = call[:call.index(")\n", call.index("fence_commit"))]
    for required in ("job_id=job_id", "execution_generation=_generation",
                     "fence_commit=", 'final_state.get("retry_count"'):
        assert required in call, f"the delivery call must pass {required}"


def test_being_fenced_produces_no_terminal_side_effects():
    body = _finalize_body()
    after = body[body.index("deliver_repair_report("):]
    fenced = after[:after.index("if final_status == JobStatus.succeeded:")]
    # the same ending artifact registration has: write nothing, publish nothing, let the current
    # owner finalize
    assert "StaleFenced" in fenced or "StaleWorkerFenced" in fenced
    assert '"status": "fenced"' in fenced and '"skipped": "not_owner"' in fenced


def test_the_delivery_cannot_change_the_runs_status():
    body = _finalize_body()
    after = body[body.index("deliver_repair_report("):]
    block = after[:after.index("if final_status == JobStatus.succeeded:")]
    # evidence is not a deliverable: nothing in this block may reassign the terminal decision
    assert "final_status =" not in block
    assert "_decision =" not in block
    assert "apply_delivery" not in block


def test_the_orchestrator_module_still_parses():
    # the edits above are asserted as source text, so prove the file is valid Python
    ast.parse(SOURCE)
