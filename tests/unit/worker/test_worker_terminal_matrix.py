# Responsibility: Verify each combination of verdict, executor success and delivery reaches the right terminal state.
from __future__ import annotations

# pyvista/PIL stand-ins are installed ONCE by tests/unit/conftest.py, and only where the
# real package is genuinely absent. Doing it per-module raced: whichever module was
# imported first decided, and one of them shadowed an installed pyvista.
import asyncio
import uuid as _uuid
from types import SimpleNamespace

import pytest
from conftest import FakePublisher, install_durable_execution_fakes  # noqa: E402

# graph → agents.reviewer.visual → sandbox.sandbox needs a renderer; stub it if absent
# (same pattern as test_graph_real.py).
import meshpipeline.application.pipeline_run as wt  # noqa: E402
import meshpipeline.pipeline.graph as graph_module  # noqa: E402
import meshpipeline.settings.runtime as rtcfg
from meshpipeline.persistence.job_state import TransitionResult  # noqa: E402
from meshpipeline.persistence.models import FailedReason, JobStatus  # noqa: E402

JOB_ID = str(_uuid.uuid4())


class _FakeResult:
    def scalars(self): return self
    def all(self): return []
    def scalar_one_or_none(self): return None
    def first(self): return None


class FakeSession:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def commit(self): pass
    async def execute(self, *a, **k): return _FakeResult()


class FakeRepo:
    def __init__(self):
        self.status_calls: list[JobStatus] = []
        self.row = SimpleNamespace(owner_id="dev-user", status=JobStatus.pending, failed_reason=None,
                                   created_at=None, ended_at=None)

    async def get_internal(self, db, job_id): return self.row
    async def get_for_owner(self, db, job_id, owner_id): return self.row
    async def transition(self, db, job_id, target, *, allow=None):
        # the fake CAS: record the target, advance the row, always "win" (rejection races are
        # covered against real Postgres in tests/integration/test_job_transitions_postgres.py)
        self.status_calls.append(target)
        self.row.status = target
        return TransitionResult.applied
    async def update_current_attempt(self, db, job_id, n): pass
    async def set_final_result(self, db, job_id, final_result):
        self.final_result = final_result


def _wire(monkeypatch, final_state: dict, upload_fails: bool = False, bad_report: bool = False):
    rec = SimpleNamespace(repo=FakeRepo(), uploads=[], dead_letters=[], published=[])

    class FakeGraph:

        async def aget_state(self, config=None):

            # A compiled graph always answers this; the entry asks before deciding fresh vs

            # resume. An empty thread is the right answer for a double that never checkpoints.

            from types import SimpleNamespace

            return SimpleNamespace(next=(), values={}, tasks=(), created_at=None, config={})

        async def ainvoke(self, state, config=None):
            return {**state, **final_state}

    monkeypatch.setattr(graph_module, "build_graph", lambda checkpointer=None: FakeGraph())

    import sqlalchemy.ext.asyncio as sa_aio
    monkeypatch.setattr(sa_aio, "create_async_engine",
                        lambda *a, **k: SimpleNamespace(dispose=_async_noop))
    monkeypatch.setattr(sa_aio, "async_sessionmaker", lambda **k: FakeSession)

    import meshpipeline.persistence.repositories.job_repository as jr
    monkeypatch.setattr(jr, "JobRepository", lambda: rec.repo)

    # the worker speaks in EVENTS now; record them as the browser would receive them
    def _fake_pub(job_id, stage="outcome"):
        p = FakePublisher(job_id, stage)
        p.events = rec.published
        return p
    monkeypatch.setattr(wt, "_pub", _fake_pub)
    monkeypatch.setattr(wt, "_incr_delivery_count", lambda job_id: 1)

    import meshpipeline.errors as failures
    monkeypatch.setattr(failures, "record_dead_letter",
                        lambda job_id, fc, dep, detail, extra=None:
                        rec.dead_letters.append((fc, dep, detail)))

    import meshpipeline.application.artifact_uploader as au
    async def fake_upload(db, job_id, ws, *, delivery_attempt=0, execution_generation=0, **_kw):
        # The fake matches PRODUCTION's contract: a required-delivery failure RAISES
        # RequiredArtifactDeliveryError (the real uploader does too - it no longer swallows), and a
        # success returns an ArtifactDeliveryReport whose required artifact is delivered. The
        # end-to-end real-uploader failure is proven in test_artifact_delivery_contract.py.
        if upload_fails:
            raise au.RequiredArtifactDeliveryError("minio unreachable")
        rec.uploads.append(str(ws))
        if bad_report:
            # a CONTRACT-VIOLATING report: claims a required artifact but delivered none, WITHOUT
            # raising. The caller's defence-in-depth must still refuse to report success.
            return au.ArtifactDeliveryReport(planned=["mesh_bundle"], required=["mesh_bundle"],
                                             delivered=[])
        return au.ArtifactDeliveryReport(
            planned=["mesh_bundle"], required=["mesh_bundle"],
            delivered=[{"type": "mesh_bundle", "storage_key": f"jobs/{job_id}/b.tar.gz",
                        "size_bytes": 10, "checksum": None}])
    monkeypatch.setattr(au, "upload_job_artifacts", fake_upload)

    monkeypatch.setattr(asyncio, "sleep", _async_noop_args)
    # the durable lease + transactional-outbox seams (the DB is faked wholesale here)
    install_durable_execution_fakes(monkeypatch, wt)
    return rec


async def _async_noop(): pass
async def _async_noop_args(*a, **k): pass


def _state(**over) -> dict:
    base = {"reviewer_verdict": "FAIL", "executor_success": False, "api_failure": "",
            "outcome_message": "done.", "retry_count": 0,
            "openfoam_workspace": "/tmp/ws-x"}
    base.update(over)
    return base


async def _run(monkeypatch, final_state, upload_fails=False, bad_report=False):
    rec = _wire(monkeypatch, final_state, upload_fails=upload_fails, bad_report=bad_report)
    result = await wt._run_async(wt.JobRequest(job_id=JOB_ID))
    return rec, result


async def test_pass_with_executor_success_succeeds_and_uploads(monkeypatch):
    rec, result = await _run(monkeypatch,
                             _state(reviewer_verdict="PASS", executor_success=True))
    assert result["status"] == "succeeded"
    assert rec.repo.status_calls[-1] == JobStatus.succeeded
    assert rec.uploads == ["/tmp/ws-x"]
    assert rec.dead_letters == []
    # the artifact-ready note is emitted only on a genuine delivery
    notes = [e.get("text", "") for e in rec.published if e["type"] == "note"]
    assert any("Packaged your mesh" in t for t in notes)
    assert any("Packaged your mesh - 1 file(s)" in t for t in notes)   # count reflects delivered


async def test_a_report_missing_its_required_artifact_does_not_succeed(monkeypatch):
    rec, result = await _run(monkeypatch,
                             _state(reviewer_verdict="PASS", executor_success=True),
                             bad_report=True)
    assert result["status"] == "failed"
    assert rec.repo.status_calls[-1] == JobStatus.failed
    notes = [e.get("text", "") for e in rec.published if e["type"] == "note"]
    assert not any("Packaged your mesh" in t for t in notes)


async def test_pass_without_executor_success_fails_and_never_uploads(monkeypatch):
    # THE delivery gate: a reviewer PASS on an unvalidated mesh must not ship.
    rec, result = await _run(monkeypatch,
                             _state(reviewer_verdict="PASS", executor_success=False))
    assert result["status"] == "failed"
    assert rec.repo.status_calls[-1] == JobStatus.failed
    assert rec.uploads == []
    assert rec.repo.row.failed_reason == FailedReason.mesh_generation


async def test_reviewer_reject_on_validated_mesh_fails_with_reviewer_reason(monkeypatch):
    rec, result = await _run(monkeypatch,
                             _state(reviewer_verdict="FAIL", executor_success=True))
    assert result["status"] == "failed"
    assert rec.uploads == []
    assert rec.repo.row.failed_reason == FailedReason.reviewer_rejected


async def test_api_failure_is_dead_lettered_and_fails(monkeypatch):
    rec, result = await _run(
        monkeypatch,
        _state(reviewer_verdict="PASS", executor_success=True,
               api_failure="<<API_FAILURE:deepseek: timeout>>"))
    assert result["status"] == "failed"
    assert rec.uploads == []
    assert len(rec.dead_letters) == 1
    assert "deepseek" in rec.dead_letters[0][2]


async def test_delivery_failure_downgrades_success_to_failed(monkeypatch):
    rec, result = await _run(monkeypatch,
                             _state(reviewer_verdict="PASS", executor_success=True,
                                    outcome_message="your mesh is ready!"),
                             upload_fails=True)
    assert result["status"] == "failed"
    assert rec.repo.status_calls[-1] == JobStatus.failed
    assert rec.uploads == []
    # the job is dead-lettered against the object store...
    assert any(dep == "minio" for _, dep, _ in rec.dead_letters)
    # ...and the success message is replaced with a blameless failure message.
    closing = [e["text"] for e in rec.published if e["type"] == "closing"]
    assert closing and "ready!" not in closing[-1]
    # EVENT TRUTHFULNESS: no artifact-ready ("Packaged your mesh") note may be emitted when
    # delivery failed - the browser must never advertise a download that does not exist.
    notes = [e.get("text", "") for e in rec.published if e["type"] == "note"]
    assert not any("Packaged your mesh" in t for t in notes)


async def test_redelivery_over_cap_dead_letters_without_running_graph(monkeypatch):
    rec = _wire(monkeypatch, _state())
    monkeypatch.setattr(wt, "_incr_delivery_count",
                        lambda job_id: rtcfg.CELERY_MAX_REDELIVERIES + 1)
    def _boom(checkpointer=None):
        raise AssertionError("graph must not run for a poison job")
    monkeypatch.setattr(graph_module, "build_graph", _boom)
    result = await wt._run_async(wt.JobRequest(job_id=JOB_ID))
    assert result["status"] == "failed" and result.get("reason") == "redelivery_cap"
    assert rec.repo.status_calls[-1] == JobStatus.failed
    assert rec.dead_letters and rec.dead_letters[0][1] == "worker"


@pytest.fixture(autouse=True)
def _execution_fence(monkeypatch):
    # This suite claims delivery with no Redis. The mirror is stood in for at the two seams
    # production resolves; ownership checks, claim transitions and the transaction stay real.
    from tests.execution_fence_double import install
    return install(monkeypatch)
