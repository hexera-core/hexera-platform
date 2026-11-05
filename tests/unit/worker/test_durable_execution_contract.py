# Responsibility: Verify ownership is a closed typed claim that never leaks its token, and exhaustion reads as timeout.
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from meshpipeline.application.final_result import (
    FailureCategory,
    ReviewExecution,
    ReviewVerdict,
    TerminalStatus,
    _derive_failure_category,
    build_final_result,
    render_message,
)
from meshpipeline.persistence.lease import ClaimResult, ExecutionOwnership
from meshpipeline.persistence.repositories.terminal_outbox_repository import dedup_key_for

APP = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"


# observability: the worker token is a SECRET; only a correlation hash may be emitted
def test_ownership_never_exposes_the_raw_token_in_logs_or_repr():
    tok = uuid.UUID("12345678-1234-5678-1234-567812345678")
    own = ExecutionOwnership(job_id=uuid.uuid4(), execution_generation=3, worker_token=tok,
                             backend="celery", pipeline_deadline_at=None)
    h = own.token_hash()
    assert len(h) == 12 and str(tok) not in h
    assert own.token_hash() == h                       # stable (a correlation id, not a nonce)


# 8 the claim decision table is DOCUMENTED and typed
def test_claim_result_is_a_closed_typed_set():
    assert {c.value for c in ClaimResult} == {
        "acquired_new_generation", "resumed_same_generation", "active_lease_conflict",
        "already_terminal", "invalid_job_state", "not_found"}


def test_terminal_dedup_key_is_one_per_job():
    jid = uuid.uuid4()
    assert dedup_key_for(jid) == dedup_key_for(str(jid))    # stable across uuid/str
    assert dedup_key_for(jid) != dedup_key_for(uuid.uuid4())


# mid-graph budget exhaustion is reported as timed_out
def test_mid_graph_exhaustion_is_classified_timed_out():
    cat = _derive_failure_category(
        executor_success=False, verdict=None, execution=ReviewExecution.not_reached, failed_gate="", api_failure="",
        required_ready=False, attempts=1, attempts_max=3, pipeline_timed_out=True)
    assert cat == FailureCategory.timed_out


def test_timed_out_does_not_mask_a_real_review_rejection_or_gate_failure():
    assert _derive_failure_category(
        executor_success=True, verdict=ReviewVerdict.failed, execution=ReviewExecution.completed, failed_gate="", api_failure="",
        required_ready=False, attempts=1, attempts_max=3,
        pipeline_timed_out=True) == FailureCategory.review_rejected
    assert _derive_failure_category(
        executor_success=False, verdict=None, execution=ReviewExecution.not_reached, failed_gate="patch_contract",
        api_failure="", required_ready=False, attempts=1, attempts_max=3,
        pipeline_timed_out=True) == FailureCategory.gate_failed


def test_timed_out_outranks_the_generic_downstream_symptom():
    assert _derive_failure_category(
        executor_success=False, verdict=None, execution=ReviewExecution.not_reached, failed_gate="",
        api_failure="deepseek: timeout", required_ready=False, attempts=1, attempts_max=3,
        pipeline_timed_out=True) == FailureCategory.timed_out


def test_timed_out_renders_a_truthful_closing_that_claims_no_download():
    fr = build_final_result(
        job_id="j", owner_id="o", status=TerminalStatus.failed, engine="cfmesh", purpose="p",
        dimensionality="3d", approved_snapshot_id="s", executor_success=False,
        reviewer_verdict="", failed_gate="", api_failure="", attempts=2, attempts_max=3,
        required_ready=False, delivered_types=[], optional_warnings=[], pipeline_timed_out=True)
    assert fr.failure_category == "timed_out"
    msg = render_message(fr)
    assert "ran out of time" in msg
    assert "No downloadable mesh deliverable is available." in msg
    for claim in ("ready to download", "completed successfully", "Review: passed"):
        assert claim not in msg


def test_a_successful_run_is_never_marked_timed_out():
    fr = build_final_result(
        job_id="j", owner_id="o", status=TerminalStatus.succeeded, engine="cfmesh", purpose="p",
        dimensionality="3d", approved_snapshot_id="s", executor_success=True,
        reviewer_verdict="PASS", failed_gate="", api_failure="", attempts=1, attempts_max=3,
        required_ready=True, delivered_types=["mesh_bundle"], optional_warnings=[],
        pipeline_timed_out=True)
    assert fr.outcome_code == "success" and fr.failure_category is None


# the PRODUCTION graph wrapper fences every node
async def test_the_graph_node_wrapper_fences_before_the_node_runs(monkeypatch):
    from meshpipeline.application import execution_fence as ef
    from meshpipeline.pipeline import graph as g

    ran: list[str] = []

    async def _node(state):
        ran.append("node")
        return {"ok": True}

    seams: list[str] = []

    async def _reject(where, **kw):
        seams.append(where)
        raise ef.StaleWorkerFenced(where, _OWN)

    monkeypatch.setattr(ef, "assert_current_owner", _reject)
    wrapped = g._fenced("node_builder", _node)
    with pytest.raises(ef.StaleWorkerFenced):
        await wrapped({"job_id": "j"})
    assert ran == []                       # the node body NEVER ran
    assert seams == ["graph node node_builder"]


async def test_the_graph_node_wrapper_runs_the_node_when_ownership_holds(monkeypatch):
    from meshpipeline.application import execution_fence as ef
    from meshpipeline.pipeline import graph as g

    ran: list[str] = []

    async def _node(state):
        ran.append("node")
        return {"ok": True}

    async def _allow(where, **kw):
        return None

    monkeypatch.setattr(ef, "assert_current_owner", _allow)
    wrapped = g._fenced("node_reviewer", _node)
    out = await wrapped({"job_id": "j"})
    assert ran == ["node"] and out == {"ok": True}


_OWN = ExecutionOwnership(job_id=uuid.uuid4(), execution_generation=1,
                          worker_token=uuid.uuid4(), backend="celery", pipeline_deadline_at=None)
