# Responsibility: Verify the terminal order is read, render, commit, publish, once, and a fenced worker writes nothing.
from __future__ import annotations

import uuid

import pytest

from meshpipeline.application.terminal_finalize import (
    FinalizeOutcome,
    TerminalAssembly,
    assemble_and_finalize,
    build_terminal_result,
    read_delivered_types,
)
from meshpipeline.persistence.models import FailedReason, JobStatus

JOB = str(uuid.uuid4())


class _Log:
    def __init__(self): self.warnings = []
    def warning(self, *a, **k): self.warnings.append(a)
    def error(self, *a, **k): pass
    def info(self, *a, **k): pass


def _assembly(**kw) -> TerminalAssembly:
    base = {"job_id": JOB, "owner_id": "owner-1", "status": JobStatus.succeeded,
            "failed_reason": None, "engine": "cfmesh", "purpose": "external_cfd",
            "dimensionality": "3D", "approved_snapshot_id": "snap-1", "executor_success": True,
            "reviewer_verdict": "PASS", "attempts": 1, "attempts_max": 4}
    base.update(kw)
    return TerminalAssembly(**base)


# pure assembly

def test_a_succeeded_run_with_its_deliverable_renders_a_success_verdict():
    out = build_terminal_result(_assembly(), delivered_types=["mesh_bundle"])
    assert out.result.status.value == "succeeded"
    assert out.closing_message.strip()
    assert out.delivered_types == ["mesh_bundle"]


def test_a_verdict_never_over_claims_a_download_it_cannot_prove():
    out = build_terminal_result(_assembly(), delivered_types=[])
    assert out.required_ready is False


def test_a_failed_run_renders_a_failure_verdict():
    out = build_terminal_result(
        _assembly(status=JobStatus.failed, failed_reason=FailedReason.reviewer_rejected,
                  reviewer_verdict="FAIL"),
        delivered_types=[])
    assert out.result.status.value == "failed"


def test_a_delivery_downgrade_carries_no_artifact_references():
    out = build_terminal_result(
        _assembly(status=JobStatus.failed, failed_reason=FailedReason.unhandled),
        delivered_types=[])
    assert out.required_ready is False
    assert "mesh_bundle" not in str(out.result.to_dict().get("delivered_types", []))


def test_the_blameless_system_note_is_the_only_pre_composed_message_honoured():
    kept = build_terminal_result(
        _assembly(status=JobStatus.failed, api_failure="openai: 500",
                  pre_composed_message="We could not reach the model provider."),
        delivered_types=[])
    assert kept.closing_message == "We could not reach the model provider."

    discarded = build_terminal_result(
        _assembly(pre_composed_message="a stale pre-delivery draft"), delivered_types=["mesh_bundle"])
    assert discarded.closing_message != "a stale pre-delivery draft", (
        "a stale outcome_message became the terminal closing")


def test_a_timed_out_run_is_reported_as_timed_out_not_its_symptom():
    out = build_terminal_result(
        _assembly(status=JobStatus.failed, failed_reason=FailedReason.mesh_generation,
                  reviewer_verdict="", executor_success=False, pipeline_timed_out=True),
        delivered_types=[])
    assert "timed_out" in str(out.result.to_dict())


def test_assembly_is_pure_and_needs_no_database():
    a = _assembly()
    one = build_terminal_result(a, delivered_types=["mesh_bundle"]).result.to_dict()
    two = build_terminal_result(a, delivered_types=["mesh_bundle"]).result.to_dict()
    one.pop("finalized_at", None); two.pop("finalized_at", None)
    assert one == two


# conservative reads

async def test_an_unreadable_artifact_table_renders_conservatively():
    class _S:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    import meshpipeline.persistence.repositories.artifact_repository as ar

    class _Boom:
        async def get_by_job(self, db, jid): raise RuntimeError("table gone")
    real = ar.ArtifactRepository
    ar.ArtifactRepository = _Boom
    try:
        log = _Log()
        assert await read_delivered_types(lambda: _S(), JOB, jlog=log) == []
        assert log.warnings, "an unreadable artifact table was not reported"
    finally:
        ar.ArtifactRepository = real


# ordering and fencing

class _Recorder:

    def __init__(self, *, fenced=False, rows=("mesh_bundle",)):
        self.order: list[str] = []
        self.fenced = fenced
        self.rows = rows
        self.finalize_calls = 0
        self.publish_calls = 0


@pytest.fixture
def wired(monkeypatch):
    rec = _Recorder()

    class _S:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def commit(self): rec.order.append("commit")

    import meshpipeline.application.outbox_publisher as obp
    import meshpipeline.application.terminal_finalize as tf
    import meshpipeline.persistence.repositories.artifact_repository as ar

    class _Rows:
        async def get_by_job(self, db, jid):
            rec.order.append("read_artifacts")
            return [type("R", (), {"artifact_type": type("T", (), {"value": v})()})()
                    for v in rec.rows]

    async def _finalize(db, **kw):
        rec.order.append("finalize")
        rec.finalize_calls += 1
        return FinalizeOutcome(fenced=rec.fenced, transition=None,
                               durable_status=None, enqueued=not rec.fenced)

    async def _publish(sf, job_id):
        rec.order.append("publish")
        rec.publish_calls += 1

    class _JobRepo:
        async def get_internal(self, db, jid):
            rec.order.append("read_timestamps")
            return type("J", (), {"created_at": None, "ended_at": None})()

    monkeypatch.setattr(ar, "ArtifactRepository", _Rows)
    monkeypatch.setattr(tf, "finalize_terminal_atomic", _finalize)
    monkeypatch.setattr(obp, "deliver_own_terminal_event", _publish)
    rec.sessions = lambda: _S()
    rec.job_repo = _JobRepo()
    return rec


async def test_the_terminal_order_is_read_render_commit_publish(wired):
    out = await assemble_and_finalize(wired.sessions, _assembly(), ownership=object(),
                                      lease_repo=object(), job_repo=wired.job_repo, jlog=_Log())
    assert not out.fenced
    steps = [s for s in wired.order if s in ("read_artifacts", "finalize", "publish")]
    assert steps == ["read_artifacts", "finalize", "publish"], wired.order
    assert wired.order.index("finalize") < wired.order.index("publish"), (
        "the terminal event was published before the terminal transaction committed")


async def test_exactly_one_terminal_transition_and_one_publication(wired):
    await assemble_and_finalize(wired.sessions, _assembly(), ownership=object(),
                                lease_repo=object(), job_repo=wired.job_repo, jlog=_Log())
    assert wired.finalize_calls == 1 and wired.publish_calls == 1


async def test_a_fenced_worker_writes_nothing_and_publishes_nothing(wired):
    wired.fenced = True
    log = _Log()
    out = await assemble_and_finalize(wired.sessions, _assembly(), ownership=object(),
                                      lease_repo=object(), job_repo=wired.job_repo, jlog=log)
    assert out.fenced and out.result is None
    assert wired.publish_calls == 0, "a superseded worker published a terminal event"
    assert "read_timestamps" not in wired.order
    assert log.warnings, "the fence was not reported"


async def test_a_fenced_worker_reports_no_timestamps(wired):
    wired.fenced = True
    out = await assemble_and_finalize(wired.sessions, _assembly(), ownership=object(),
                                      lease_repo=object(), job_repo=wired.job_repo, jlog=_Log())
    assert out.created_at is None and out.ended_at is None


async def test_a_second_invocation_finalizes_again_only_through_the_atomic_call(wired):
    for _ in range(2):
        await assemble_and_finalize(wired.sessions, _assembly(), ownership=object(),
                                    lease_repo=object(), job_repo=wired.job_repo, jlog=_Log())
    assert wired.finalize_calls == 2, "a duplicate call bypassed the atomic transaction"
    assert wired.publish_calls == 2


async def test_a_publication_failure_does_not_undo_the_commit(wired, monkeypatch):
    import meshpipeline.application.outbox_publisher as obp

    async def _boom(sf, job_id):
        wired.order.append("publish")
        raise RuntimeError("redis down")
    monkeypatch.setattr(obp, "deliver_own_terminal_event", _boom)
    with pytest.raises(RuntimeError):
        await assemble_and_finalize(wired.sessions, _assembly(), ownership=object(),
                                    lease_repo=object(), job_repo=wired.job_repo, jlog=_Log())
    assert "finalize" in wired.order, "the commit did not happen before publication was attempted"


# mutation guard

def test_the_orchestrator_no_longer_assembles_or_commits_the_terminal_outcome():
    import inspect

    import meshpipeline.application.pipeline_run as pr

    src = inspect.getsource(pr)
    assert "assemble_and_finalize" in src, "pipeline_run no longer delegates terminal assembly"
    assert "ArtifactRepository" not in src, "pipeline_run reads the artifact rows itself again"
    assert "artifact_policy" not in src, "pipeline_run applies the readiness policy again"
    assert "finalize_terminal_atomic" not in src.split("except Exception as exc:")[0], (
        "the SUCCESS path commits the terminal transaction itself again")
    # The crash path still builds its own verdict; it is extracted in the next commit and has its
    # own owning tests. Recorded here so this guard is not silently satisfied by that duplication.
    assert src.count("build_final_result") <= 1
