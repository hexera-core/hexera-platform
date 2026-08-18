# Responsibility: Verify no stale pass, malformed verdict or lost claim can turn a failed run into a success.
from __future__ import annotations

# pyvista/PIL stand-ins are installed ONCE by tests/unit/conftest.py, and only where the
# real package is genuinely absent. Doing it per-module raced: whichever module was
# imported first decided, and one of them shadowed an installed pyvista.
import asyncio
import uuid as _uuid
from types import SimpleNamespace

import pytest
from conftest import FakePublisher, install_durable_execution_fakes  # noqa: E402

import meshpipeline.application.pipeline_run as wt  # noqa: E402
import meshpipeline.pipeline.graph as graph_module  # noqa: E402
from meshpipeline.persistence.job_state import TransitionResult  # noqa: E402
from meshpipeline.persistence.models import JobStatus  # noqa: E402

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
    def __init__(self, cas_loss_status: JobStatus | None = None):
        self.status_calls: list[JobStatus] = []
        self.final_result: dict | None = None
        # cas_loss_status simulates a COMPETING terminal winner (e.g. the reaper) that finalizes the
        # job WHILE this worker is finishing: the intake transitions apply normally, but OUR terminal
        # transition is rejected and the durable row already holds the winner's terminal status.
        self._cas_loss = cas_loss_status
        self.row = SimpleNamespace(owner_id="dev-user", status=JobStatus.pending, failed_reason=None,
                                   created_at=None, ended_at=None, final_result=None)

    async def get_internal(self, db, job_id): return self.row
    async def get_for_owner(self, db, job_id, owner_id): return self.row
    async def transition(self, db, job_id, target, *, allow=None):
        self.status_calls.append(target)
        if self._cas_loss is not None and target in (JobStatus.succeeded, JobStatus.failed):
            # a competing winner got here first: reject our terminal write and expose its status
            self.row.status = self._cas_loss
            return TransitionResult.rejected_current_state
        self.row.status = target
        return TransitionResult.applied
    async def update_current_attempt(self, db, job_id, n): pass
    async def set_final_result(self, db, job_id, final_result):
        self.final_result = final_result
        self.row.final_result = final_result


async def _async_noop(): pass
async def _async_noop_args(*a, **k): pass


def _wire(monkeypatch, final_state: dict, upload_fails: bool = False,
          cas_loss_status: JobStatus | None = None):
    rec = SimpleNamespace(repo=FakeRepo(cas_loss_status), uploads=[], dead_letters=[], published=[])

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
        if upload_fails:
            raise au.RequiredArtifactDeliveryError("minio unreachable")
        rec.uploads.append(str(ws))
        return au.ArtifactDeliveryReport(
            planned=["mesh_bundle"], required=["mesh_bundle"],
            delivered=[{"type": "mesh_bundle", "storage_key": f"jobs/{job_id}/b.tar.gz",
                        "size_bytes": 10, "checksum": None}])
    monkeypatch.setattr(au, "upload_job_artifacts", fake_upload)

    monkeypatch.setattr(asyncio, "sleep", _async_noop_args)
    # durable lease + transactional-outbox seams (the DB is faked wholesale here)
    install_durable_execution_fakes(monkeypatch, wt)
    return rec


def _state(**over) -> dict:
    base = {"reviewer_verdict": "FAIL", "executor_success": False, "api_failure": "",
            "outcome_message": "", "retry_count": 0, "engine": "cfmesh",
            "openfoam_workspace": "/tmp/ws-x"}
    base.update(over)
    return base


async def _run(monkeypatch, final_state, upload_fails=False, cas_loss_status=None):
    rec = _wire(monkeypatch, final_state, upload_fails=upload_fails,
                cas_loss_status=cas_loss_status)
    result = await wt._run_async(wt.JobRequest(job_id=JOB_ID))
    _closing = [e["text"] for e in rec.published if e["type"] == "closing"]
    return rec, result, (_closing[-1] if _closing else "")


_SUCCESS_MARKERS = ("completed successfully", "ready to download", "Review: passed")


def _assert_not_a_success(closing: str, fr: dict):
    assert "did not complete successfully" in closing
    for m in _SUCCESS_MARKERS:
        assert m not in closing, f"success marker {m!r} leaked into a failure closing"
    assert fr is not None and fr["outcome_code"] != "success"
    assert fr["required_ready"] is False


# 1. a failed hard gate defeats a reviewer PASS

async def test_failed_patch_gate_defeats_reviewer_pass(monkeypatch):
    rec, result, closing = await _run(monkeypatch, _state(
        reviewer_verdict="PASS", executor_success=False, executor_failed_gate="patch_contract"))
    assert result["status"] == "failed"
    fr = rec.repo.final_result
    _assert_not_a_success(closing, fr)
    assert fr["failure_category"] == "gate_failed"
    assert fr["patch_contract_ok"] is False        # never claims boundaries were preserved
    assert "preserved" not in closing


# 2. a stale reviewer PASS on an unvalidated mesh cannot authorize a success

async def test_stale_reviewer_pass_without_executor_success_cannot_authorize(monkeypatch):
    rec, result, closing = await _run(monkeypatch, _state(
        reviewer_verdict="PASS", executor_success=False))
    assert result["status"] == "failed"
    fr = rec.repo.final_result
    _assert_not_a_success(closing, fr)
    # the PASS is discarded, and the record says the review was never reached
    assert fr["reviewer_verdict"] is None and fr["review_execution"] == "not_reached"


# 3. an exhausted attempt loop ends as a deterministic failure

async def test_exhausted_attempts_is_deterministic_failure(monkeypatch):
    _max = int(wt.bcfg.BUILDER_MAX_TOTAL_ATTEMPTS)
    rec, result, closing = await _run(monkeypatch, _state(
        reviewer_verdict="", executor_success=False, retry_count=_max))
    assert result["status"] == "failed"
    fr = rec.repo.final_result
    _assert_not_a_success(closing, fr)
    assert fr["failure_category"] == "attempts_exhausted"
    assert f"{_max}/{_max}" in closing


# 4. a malformed reviewer verdict can never become a PASS

async def test_malformed_reviewer_verdict_cannot_become_success(monkeypatch):
    # A verdict string that CONTAINS "PASS" and success words but is not the exact token.
    rec, result, closing = await _run(monkeypatch, _state(
        reviewer_verdict="PASS - looks great, your download is ready!", executor_success=True))
    assert result["status"] == "failed"
    fr = rec.repo.final_result
    _assert_not_a_success(closing, fr)
    assert "download is ready" not in closing      # the model's words never reach the user
    assert fr["reviewer_verdict"] != "passed"


# 5. the executed engine is the only engine named; nothing else reaches the verdict

async def test_only_the_executed_engine_is_named(monkeypatch):
    rec, result, closing = await _run(monkeypatch, _state(
        reviewer_verdict="PASS", executor_success=True, engine="cfmesh"))
    # succeeds (delivery ok in this wiring); the durable engine fact is the executed one
    assert result["status"] == "succeeded"
    fr = rec.repo.final_result
    assert fr["engine"] == "cfmesh"
    assert "cfmesh" in closing and "gmsh" not in closing and "snappy" not in closing


# 6. a pre-composed message can NEVER override executor_success/verdict

async def test_stale_success_draft_never_survives_a_failed_run(monkeypatch):
    # A success-sounding draft was left in state, but the run FAILED and there is no
    # api_failure - the draft must be discarded for the deterministic failure verdict.
    draft = "Great news - your mesh succeeded and is ready to download!"
    rec, result, closing = await _run(monkeypatch, _state(
        reviewer_verdict="FAIL", executor_success=False, outcome_message=draft))
    assert result["status"] == "failed"
    fr = rec.repo.final_result
    _assert_not_a_success(closing, fr)
    assert draft not in closing
    assert "succeeded and is ready" not in closing


# 7. a success with no DURABLE ready row never advertises a download

async def test_success_without_ready_rows_does_not_claim_a_download(monkeypatch):
    # The upload path ran, but the durable ready-artifact query is the source of truth for the
    # download claim. With no ready rows visible, the verdict must say "prepared", never "ready to
    # download" - claims bind to Artifact rows, not to the delivery plan.
    rec, result, closing = await _run(monkeypatch, _state(
        reviewer_verdict="PASS", executor_success=True))
    assert result["status"] == "succeeded"
    fr = rec.repo.final_result
    assert fr["required_ready"] is False
    assert "ready to download" not in closing
    assert "prepared" in closing


# 8. a lost terminal CAS renders from the DURABLE winner, not our intended status

async def test_cas_loss_leaves_the_durable_winner_authoritative(monkeypatch):
    rec, result, closing = await _run(monkeypatch, _state(
        reviewer_verdict="PASS", executor_success=True),
        cas_loss_status=JobStatus.failed)
    assert rec.repo.final_result is None            # the winner's record was NOT overwritten
    assert rec.repo.row.status == JobStatus.failed  # the durable winner still stands
    # and the loser announced nothing - no success prose reached the user
    for m in _SUCCESS_MARKERS:
        assert m not in closing


async def test_a_system_failure_note_is_the_only_honoured_precomposed_message(monkeypatch):
    # The ONE legitimate pre-composed message: node_failure_handler's blameless system-failure note,
    # honoured only because api_failure is set. It is application-owned, not model prose.
    note = "A required service was temporarily unavailable. Please try again shortly."
    rec, result, closing = await _run(monkeypatch, _state(
        reviewer_verdict="PASS", executor_success=True, outcome_message=note,
        api_failure="<<API_FAILURE:deepseek: timeout>>"))
    assert result["status"] == "failed"
    assert closing == note                          # honoured verbatim (system-failure sink)
    for m in _SUCCESS_MARKERS:
        assert m not in closing


@pytest.fixture(autouse=True)
def _execution_fence(monkeypatch):
    # This suite claims delivery with no Redis. The mirror is stood in for at the two seams
    # production resolves; ownership checks, claim transitions and the transaction stay real.
    from tests.execution_fence_double import install
    return install(monkeypatch)
