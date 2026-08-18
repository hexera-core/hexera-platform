# Responsibility: Verify artifacts are reported once, a transient failure retried, a sustained one downgrading the run.
from __future__ import annotations

import uuid

import pytest

from meshpipeline.application import artifact_uploader as AU
from meshpipeline.persistence.models import FailedReason

JOB = str(uuid.uuid4())


class _Log:
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


class _Pub:
    calls: list = []
    def __init__(self, job_id): self.job_id = job_id
    def note(self, msg, op_id=""): _Pub.calls.append(("note", op_id))
    def error(self, msg, op_id=""): _Pub.calls.append(("error", op_id))


class _Report:
    def __init__(self, delivered, ok=True): self.delivered = delivered; self._ok = ok; self.orphans = []
    def required_all_delivered(self): return self._ok


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    _Pub.calls = []

    class _NoSleep:
        @staticmethod
        async def sleep(_seconds): return None

    monkeypatch.setattr(AU, "asyncio", _NoSleep)


async def _noop_orphans(*a, **k): return None


def _sessions():
    class _S:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def commit(self): return None
    return _S


async def _run(monkeypatch, *, upload, workspace="/ws"):
    monkeypatch.setattr(AU, "upload_job_artifacts", upload)
    return await AU.deliver_succeeded_run(
        _sessions(), job_id=JOB, owner_id="o", workspace=workspace, engine="cfmesh",
        delivery_attempt=0, execution_generation=1, jlog=_Log(), publish=_Pub,
        persist_orphans=_noop_orphans)


async def test_a_delivered_run_reports_its_artifacts_once(monkeypatch):
    async def _up(*a, **k): return _Report([{"k": "mesh"}])
    out = await _run(monkeypatch, upload=_up)
    assert out.succeeded and out.delivered == [{"k": "mesh"}] and out.failed_reason is None
    assert [c for c in _Pub.calls if c[1] == "delivered"] == [("note", "delivered")], (
        "the delivery announcement is not exactly once per run")


async def test_no_workspace_is_a_delivery_failure_not_a_silent_success(monkeypatch):
    async def _up(*a, **k): raise AssertionError("must not upload without a workspace")
    out = await _run(monkeypatch, upload=_up, workspace="")
    assert not out.succeeded and out.delivered == []
    assert out.failed_reason is not None and isinstance(out.failed_reason, FailedReason)
    assert ("error", "delivery-failed") in _Pub.calls


async def test_a_transient_failure_is_retried_then_succeeds(monkeypatch):
    calls = {"n": 0}
    async def _up(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("store blip")
        return _Report([{"k": "mesh"}])
    out = await _run(monkeypatch, upload=_up)
    assert out.succeeded and calls["n"] == 3


async def test_a_sustained_failure_downgrades_the_run(monkeypatch):
    async def _up(*a, **k): raise RuntimeError("store down")
    out = await _run(monkeypatch, upload=_up)
    assert not out.succeeded and out.delivered == [] and out.error is not None
    assert out.failed_reason is not None


async def test_a_partial_report_is_refused_even_on_a_normal_return(monkeypatch):
    async def _up(*a, **k): return _Report([{"k": "mesh"}], ok=False)
    out = await _run(monkeypatch, upload=_up)
    assert not out.succeeded, "a partial delivery was reported as success"


async def test_a_failed_delivery_reports_no_artifacts(monkeypatch):
    async def _up(*a, **k): raise RuntimeError("nope")
    out = await _run(monkeypatch, upload=_up)
    assert out.delivered == []


def test_the_orchestrator_no_longer_owns_the_retry_or_the_downgrade():
    import inspect

    import meshpipeline.application.pipeline_run as pr

    src = inspect.getsource(pr)
    assert "upload_job_artifacts" not in src, "pipeline_run uploads artifacts itself again"
    assert "delivery-failed" not in src, "pipeline_run owns the delivery failure message again"
    assert "deliver_succeeded_run" in src, "pipeline_run no longer delegates delivery"
