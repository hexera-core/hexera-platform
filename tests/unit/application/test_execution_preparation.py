# Responsibility: Verify preparation materialises geometry per disposition, and a classified failure refuses durably.
from __future__ import annotations

import uuid

import pytest

from meshpipeline.application import geometry_materializer as gm
from meshpipeline.contracts.geometry_source import GeometrySourceError
from meshpipeline.errors import FailureClass

JOB = str(uuid.uuid4())


class _Log:
    def __init__(self): self.errors = []; self.infos = []
    def error(self, *a, **k): self.errors.append(a)
    def info(self, *a, **k): self.infos.append(a)
    def warning(self, *a, **k): pass


class _S:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def commit(self): return None


class _Repo:
    def __init__(self): self.transitions = []; self.row = type("R", (), {"failed_reason": None})()
    async def transition(self, db, jid, status):
        from meshpipeline.persistence.job_state import TransitionResult
        self.transitions.append(status)
        return TransitionResult.applied
    async def get_internal(self, db, jid): return self.row


async def _prep(monkeypatch, *, disposition="pending", raises=None, published=None, repo=None):
    async def _classify(thread): return disposition

    async def _materialize(src, interp, *, job_id):
        if raises is not None:
            raise raises
        return object()

    monkeypatch.setattr(gm, "prepare_execution_geometry", _materialize)
    return await gm.prepare_for_execution(
        lambda: _S(),
        job_id=JOB, geometry_source=object(), geometry_interpretation=object(),
        classify_checkpoint=_classify, checkpoint_thread="t",
        job_repo=repo if repo is not None else _Repo(), jlog=_Log(),
        publish=(published.append if published is not None else (lambda m: None)))


async def test_a_pending_thread_materializes_its_geometry(monkeypatch):
    out = await _prep(monkeypatch, disposition="pending")
    assert out.prepared and out.materialized is not None and out.disposition == "pending"


async def test_a_completed_thread_does_no_geometry_work(monkeypatch):
    out = await _prep(monkeypatch, disposition="complete")
    assert out.prepared and out.materialized is None and out.disposition == "complete"


async def test_the_disposition_is_carried_out_for_the_graph_input_step(monkeypatch):
    for d in ("absent", "pending", "complete"):
        assert (await _prep(monkeypatch, disposition=d)).disposition == d


@pytest.mark.parametrize("dependency,reason", [
    ("geometry_source", "geometry_unavailable"),
    ("checkpointer", "checkpoint_unreadable"),
])
async def test_a_classified_failure_refuses_with_the_right_reason(monkeypatch, dependency, reason):
    exc = GeometrySourceError("nope")
    exc.dependency = dependency
    exc.failure_class = FailureClass.DEPENDENCY_DOWN
    out = await _prep(monkeypatch, raises=exc)
    assert not out.prepared
    assert out.refusal == {"job_id": JOB, "status": "failed", "reason": reason}


async def test_an_unclassified_failure_is_never_blamed_on_the_user(monkeypatch):
    out = await _prep(monkeypatch, raises=GeometrySourceError("no class attached"))
    assert not out.prepared
    assert out.refusal["reason"] in ("geometry_unavailable", "checkpoint_unreadable")


async def test_a_refusal_publishes_a_message_that_does_not_leak_the_detail(monkeypatch):
    published: list = []
    exc = GeometrySourceError("bucket s3://internal/secret-key missing")
    exc.dependency = "geometry_source"
    exc.failure_class = FailureClass.DEPENDENCY_DOWN
    await _prep(monkeypatch, raises=exc, published=published)
    assert published, "the user was told nothing"
    assert "secret-key" not in published[0] and "s3://" not in published[0]


async def test_a_refusal_records_a_dead_letter(monkeypatch):
    seen: list = []
    import meshpipeline.errors as errs
    monkeypatch.setattr(errs, "record_dead_letter", lambda *a, **k: seen.append(a))
    await _prep(monkeypatch, raises=GeometrySourceError("gone"))
    assert seen


async def test_a_refusal_is_DURABLE_not_just_a_returned_dict(monkeypatch):
    from meshpipeline.persistence.models import JobStatus

    repo = _Repo()
    exc = GeometrySourceError("checksum disagrees")
    exc.dependency = "geometry_source"
    exc.failure_class = FailureClass.DEPENDENCY_DOWN
    out = await _prep(monkeypatch, raises=exc, repo=repo)
    assert not out.prepared
    assert JobStatus.failed in repo.transitions, (
        "the refused run was never transitioned to failed - its row stays `running`")
    assert repo.row.failed_reason is not None, "the refusal recorded no reason"


async def test_a_database_that_refuses_the_transition_does_not_undo_the_refusal(monkeypatch):
    class _Boom(_Repo):
        async def transition(self, *a, **k): raise RuntimeError("db down")

    out = await _prep(monkeypatch, raises=GeometrySourceError("gone"), repo=_Boom())
    assert not out.prepared and out.refusal["status"] == "failed"


def test_the_orchestrator_no_longer_classifies_preparation_failures():
    import inspect

    import meshpipeline.application.pipeline_run as pr

    src = inspect.getsource(pr._run_async)
    assert "GeometrySourceError" not in src, "the run classifies preparation failures again"
    assert "geometry_unavailable" not in src, "the run derives the refusal reason again"
    assert "prepare_for_execution" in src
