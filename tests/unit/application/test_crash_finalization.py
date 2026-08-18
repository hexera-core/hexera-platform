# Responsibility: Verify a crash is classified, recorded once and closed truthfully, claiming no delivery.
# Boundaries: cancellation and shutdown are not caught, and a stale worker terminalizes nothing.
from __future__ import annotations

import asyncio
import uuid

import pytest

from meshpipeline.application.terminal_finalize import (
    FinalizeOutcome,
    TerminalAssembly,
    classify_crash,
    durable_facts_after_crash,
    finalize_crash,
)
from meshpipeline.errors import FailureClass, SystemFailure
from meshpipeline.persistence.models import FailedReason, JobStatus

JOB = str(uuid.uuid4())
APPROVED = {"engine": "cfmesh", "purpose": "external_cfd", "dimensionality": "3D",
            "approved_snapshot_id": "snap-1"}


class _Log:
    def __init__(self): self.warnings = []; self.errors = []
    def warning(self, *a, **k): self.warnings.append(a)
    def error(self, *a, **k): self.errors.append(a)
    def info(self, *a, **k): pass


def _defaults() -> TerminalAssembly:
    return TerminalAssembly(job_id=JOB, owner_id="o", status=JobStatus.failed,
                            failed_reason=None, attempts_max=4)


# classification

def test_a_system_failure_carries_its_own_classification():
    exc = SystemFailure("minio", FailureClass.DEPENDENCY_DOWN, "connect timeout")
    c = classify_crash(exc)
    assert c.failure_class is FailureClass.DEPENDENCY_DOWN
    assert c.dependency == "minio" and c.operator_detail == "connect timeout"
    assert isinstance(c.failed_reason, FailedReason)
    assert c.user_message and "connect timeout" not in c.user_message, (
        "the operator detail leaked into the user-facing message")


def test_an_arbitrary_exception_is_classified_best_effort():
    c = classify_crash(ValueError("boom"))
    assert c.failure_class is not None and c.dependency
    assert isinstance(c.failed_reason, FailedReason)


def test_the_user_message_never_carries_the_exception_text():
    c = classify_crash(RuntimeError("stack trace: /srv/secret/path.py line 4"))
    assert "secret" not in c.user_message and "stack" not in c.user_message.lower()


# durable facts

class _S:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def commit(self): return None


class _JobRepo:
    def __init__(self, attempt=2): self.attempt = attempt
    async def get_internal(self, db, jid):
        return type("J", (), {"current_attempt": self.attempt,
                              "created_at": None, "ended_at": None})()


class _Graph:
    def __init__(self, values=None, boom=False): self.values = values or {}; self.boom = boom
    async def aget_state(self, cfg):
        if self.boom:
            raise RuntimeError("checkpoint unreadable")
        return type("Snap", (), {"values": self.values})()


async def test_a_finished_stage_is_reported_from_the_checkpoint_not_rewritten_as_failure():
    facts = await durable_facts_after_crash(
        lambda: _S(), job_id=JOB, approved=APPROVED,
        graph=_Graph({"executor_success": True, "engine": "cfmesh"}),
        graph_config={"configurable": {}}, job_repo=_JobRepo(), jlog=_Log())
    assert facts["executor_success"] is True, (
        "a completed executor stage was rewritten as unsuccessful on the crash path")
    assert facts["engine"] == "cfmesh"


async def test_an_unreadable_checkpoint_falls_back_to_the_approved_intent():
    log = _Log()
    facts = await durable_facts_after_crash(
        lambda: _S(), job_id=JOB, approved=APPROVED, graph=_Graph(boom=True),
        graph_config={"configurable": {}}, job_repo=_JobRepo(), jlog=log)
    assert facts["engine"] == "cfmesh", "the approved intent was lost"
    assert log.warnings, "the unreadable checkpoint was not reported"


async def test_a_crash_before_the_graph_existed_still_reports_the_approved_intent():
    facts = await durable_facts_after_crash(
        lambda: _S(), job_id=JOB, approved=APPROVED, graph=None, graph_config=None,
        job_repo=_JobRepo(), jlog=_Log())
    assert facts["engine"] == "cfmesh"


async def test_an_unreadable_attempt_count_never_fails_the_verdict():
    class _Boom:
        async def get_internal(self, db, jid): raise RuntimeError("db gone")
    log = _Log()
    facts = await durable_facts_after_crash(
        lambda: _S(), job_id=JOB, approved=APPROVED, graph=_Graph(),
        graph_config={"configurable": {}}, job_repo=_Boom(), jlog=log)
    assert facts is not None and log.warnings


# persistence + fence

@pytest.fixture
def wired(monkeypatch):
    rec = type("R", (), {})()
    rec.order = []; rec.fenced = False; rec.finalize_boom = False
    rec.published = 0; rec.direct = 0; rec.finalize_kwargs = []

    import meshpipeline.application.outbox_publisher as obp
    import meshpipeline.application.terminal_finalize as tf

    async def _finalize(db, **kw):
        rec.order.append("finalize")
        rec.finalize_kwargs.append(kw)
        if rec.finalize_boom:
            raise RuntimeError("db down")
        return FinalizeOutcome(fenced=rec.fenced, transition=None, durable_status=None,
                               enqueued=not rec.fenced)

    async def _publish(sf, job_id):
        rec.order.append("publish"); rec.published += 1

    monkeypatch.setattr(tf, "finalize_terminal_atomic", _finalize)
    monkeypatch.setattr(obp, "deliver_own_terminal_event", _publish)

    def _direct(fc):
        rec.order.append("direct_closing"); rec.direct += 1
    rec.direct_closing = _direct
    return rec


async def _crash(wired, exc=RuntimeError("boom"), *, ownership=object()):
    return await finalize_crash(
        lambda: _S(), exc, job_id=JOB, owner_id="o", assembly_defaults=_defaults(),
        approved=APPROVED, graph=_Graph({"executor_success": True}),
        graph_config={"configurable": {}}, ownership=ownership, lease_repo=object(),
        job_repo=_JobRepo(), jlog=_Log(), publish=wired.direct_closing)


async def test_a_crash_is_recorded_durably_then_published(wired):
    out = await _crash(wired)
    assert out.finalized and not out.fenced
    assert wired.order == ["finalize", "publish"], wired.order
    assert wired.direct == 0, "the fallback fired even though the durable path worked"


async def test_a_stale_worker_never_terminalizes_a_job_a_newer_generation_owns(wired):
    wired.fenced = True
    out = await _crash(wired)
    assert out.fenced and not out.finalized
    assert wired.published == 0, "a superseded worker published a terminal event on the crash path"
    assert wired.direct == 0, "a fence triggered the direct-closing fallback"
    assert not out.needs_direct_closing, "a fence was treated as a failure to record"


async def test_a_broken_durable_path_still_gives_the_user_a_closing(wired):
    wired.finalize_boom = True
    out = await _crash(wired)
    assert not out.finalized and not out.fenced and out.needs_direct_closing
    assert wired.direct == 1, "the user was left without any closing"


async def test_the_crash_verdict_never_claims_a_delivery(wired):
    import inspect

    import meshpipeline.application.terminal_finalize as tf
    assert "delivered_types=[]" in inspect.getsource(tf.finalize_crash)
    await _crash(wired)


async def test_exactly_one_terminal_transition_on_the_crash_path(wired):
    await _crash(wired)
    assert wired.order.count("finalize") == 1


async def test_a_crash_is_never_recorded_as_success(wired):
    await _crash(wired)
    kw = wired.finalize_kwargs[0]
    assert kw["intended_status"] is JobStatus.failed, (
        f"a crashed run was recorded as {kw['intended_status']}")
    assert kw["failed_reason"] is not None, "a crashed run was recorded with no failure reason"


async def test_the_crash_closing_is_the_classified_user_message_not_the_exception(wired):
    await _crash(wired, RuntimeError("/srv/secret/path.py exploded"))
    closing = wired.finalize_kwargs[0]["closing_message"]
    assert closing and "secret" not in closing and "exploded" not in closing


# what must NOT be caught

def test_cancellation_and_shutdown_are_not_caught_by_the_run_handler():
    for kind in (asyncio.CancelledError, SystemExit, KeyboardInterrupt):
        assert issubclass(kind, BaseException) and not issubclass(kind, Exception), (
            f"{kind.__name__} became an Exception - the run's `except Exception` would swallow it "
            f"and manufacture a terminal record for a run that was cancelled, not failed")


def test_the_handler_catches_exception_only():
    import inspect

    import meshpipeline.application.pipeline_run as pr

    src = inspect.getsource(pr._run_async)
    assert "except BaseException" not in src, (
        "the run handler widened to BaseException - cancellation would stop propagating")
    assert "except Exception as exc:" in src


def test_the_primary_exception_is_re_raised():
    import inspect

    import meshpipeline.application.pipeline_run as pr

    # the OUTER handler is the last one in the function
    tail = inspect.getsource(pr._run_async).rsplit("except Exception as exc:", 1)[1]
    assert "\n        raise\n" in tail, "the crash is swallowed instead of re-raised"


def test_cleanup_runs_on_every_exit_path():
    import inspect

    import meshpipeline.application.pipeline_run as pr

    src = inspect.getsource(pr._run_async)
    assert "finally:" in src and "_worker_engine.dispose()" in src.split("finally:")[-1], (
        "the worker engine is not disposed from a finally - a crash would leak the pool")


# mutation guard

def test_the_orchestrator_no_longer_classifies_or_records_the_crash():
    import inspect

    import meshpipeline.application.pipeline_run as pr

    # Scoped to the run coroutine: run_pipeline legitimately classifies a checkpointer failure of
    # its own, and that is a different boundary from the crash record this stage moved.
    handler = inspect.getsource(pr._run_async).rsplit("except Exception as exc:", 1)[1]
    assert "classify_exception" not in handler, "the run handler classifies crashes again"
    assert "merge_durable_facts" not in handler, "the run handler rebuilds the crash verdict again"
    assert "record_dead_letter" not in handler, "the run handler records the dead letter again"
    assert "finalize_crash" in handler, "the run handler no longer delegates crash finalization"
