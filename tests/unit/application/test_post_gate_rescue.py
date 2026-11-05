# Responsibility: Verify that once the executor has passed a mesh, ANY later exception - a reviewer
# crash, a checkpoint write, anything - delivers the mesh with a note that the review did not
# conclude, and never fails the job; and that every case outside that rule is raised as before.
# Boundaries: a stood-in graph; the terminal rules are the real ones.
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from meshpipeline.application import post_gate_rescue as R

PASSED = {"executor_success": True, "executor_failed_gate": "", "reviewer_verdict": "",
          "engine": "gmsh", "purpose": "structural", "retry_count": 0}


def _reviewer_crash() -> Exception:
    try:
        f"{None:g}"                       # lp_base, agents/reviewer/visual.py:80
    except TypeError as exc:
        return exc
    raise AssertionError("expected a TypeError")


@pytest.mark.parametrize("state,rescued", [
    (PASSED, True),
    ({**PASSED, "reviewer_verdict": "PASS"}, True),                  # crash after the review
    ({**PASSED, "executor_success": False}, False),                  # the gates never passed
    ({**PASSED, "executor_failed_gate": "mesh_quality"}, False),
    ({**PASSED, "reviewer_verdict": "FAIL"}, False),                  # the reviewer rejected it
    ({**PASSED, "reviewer_rebuild_required": True}, False),
    ({**PASSED, "user_dispute": {"of_job_id": "x"}}, False),
    (None, False),
])
def test_the_rescue_rule(state, rescued):
    out = R.rescue(state, _reviewer_crash())
    assert (out is not None) is rescued
    if out is not None:
        assert out["executor_success"] is True
        assert out.get("api_failure", "") == ("" if state.get("reviewer_verdict") else R.REVIEWER_CRASHED)


def test_a_fence_refusal_or_a_drain_is_never_rescued():
    from meshpipeline.application.worker_handoff import WorkerDraining
    from meshpipeline.contracts.execution_guard import StaleWorkerFenced

    assert R.rescue(PASSED, StaleWorkerFenced("w", "job", 1, "t")) is None
    assert R.rescue(PASSED, WorkerDraining()) is None


class _Graph:
    def __init__(self, exc: BaseException, saved: dict | None, read_fails: bool = False):
        self.exc, self.saved, self.read_fails = exc, saved, read_fails

    async def ainvoke(self, graph_input, config=None):
        raise self.exc

    async def aget_state(self, config):
        if self.read_fails:
            raise RuntimeError("cannot read")
        return SimpleNamespace(values=self.saved)


def _log():
    return SimpleNamespace(error=lambda *a, **k: None, warning=lambda *a, **k: None)


def test_a_reviewer_that_raises_after_the_gates_passed_delivers_the_mesh():
    exc = _reviewer_crash()
    out = asyncio.run(R.run_to_end(_Graph(exc, dict(PASSED)), {}, {}, jlog=_log()))
    assert out["api_failure"] == R.REVIEWER_CRASHED and out["executor_success"] is True

    # ... and the terminal rules deliver it, saying the review did not conclude
    from meshpipeline.application.final_result import (
        RunOutcome,
        derive_terminal_status,
        review_inconclusive_caveat,
    )
    from meshpipeline.persistence.models import JobStatus

    outcome = RunOutcome.from_graph_state(out)
    assert review_inconclusive_caveat(outcome)["marker"] == R.REVIEWER_CRASHED
    import logging
    decision = derive_terminal_status(outcome, job_id="lp_base", jlog=logging.getLogger("t"))
    assert decision.status == JobStatus.succeeded

    from meshpipeline.contracts.failure_cause import review_stopped_reason
    assert "nothing about the mesh was found wrong" in review_stopped_reason(R.REVIEWER_CRASHED)
    from meshpipeline.errors import FailureClass, classify_api_failure
    assert classify_api_failure(R.REVIEWER_CRASHED) == FailureClass.REVIEW_EVIDENCE_MISSING


def test_anything_before_the_gates_passed_still_fails_as_before():
    exc = RuntimeError("builder blew up")
    with pytest.raises(RuntimeError, match="builder blew up"):
        asyncio.run(R.run_to_end(_Graph(exc, {**PASSED, "executor_success": False}), {}, {}, jlog=_log()))


def test_an_unreadable_last_state_raises_the_original_crash():
    exc = _reviewer_crash()
    with pytest.raises(TypeError):
        asyncio.run(R.run_to_end(_Graph(exc, None, read_fails=True), {}, {}, jlog=_log()))


def test_a_fence_refusal_is_raised_without_reading_anything():
    from meshpipeline.contracts.execution_guard import StaleWorkerFenced

    g = _Graph(StaleWorkerFenced("w", "job", 1, "t"), dict(PASSED), read_fails=True)
    with pytest.raises(StaleWorkerFenced):
        asyncio.run(R.run_to_end(g, {}, {}, jlog=_log()))


def test_the_run_uses_the_rescue_on_both_paths():
    import inspect

    import meshpipeline.application.pipeline_run as pr
    src = inspect.getsource(pr._run_async)
    assert "graph.ainvoke(" not in src, "a graph run that bypasses post_gate_rescue"
    assert src.count("run_to_end") >= 2


@pytest.mark.parametrize("purpose", ["structural", "internal_cfd", "external_cfd"])
def test_a_rescued_run_is_delivered_for_every_purpose(purpose):
    import logging

    from meshpipeline.application.final_result import RunOutcome, derive_terminal_status
    from meshpipeline.persistence.models import JobStatus

    out = R.rescue({**PASSED, "purpose": purpose}, _reviewer_crash())
    decision = derive_terminal_status(RunOutcome.from_graph_state(out), job_id="j", jlog=logging.getLogger("t"))
    assert decision.status == JobStatus.succeeded
