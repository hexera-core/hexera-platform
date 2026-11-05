# Responsibility: Verify no model prose survives the terminal boundary, and the published message matches the record.
from __future__ import annotations

# pyvista/PIL stand-ins are installed ONCE by tests/unit/conftest.py, and only where the
# real package is genuinely absent. Doing it per-module raced: whichever module was
# imported first decided, and one of them shadowed an installed pyvista.
import asyncio
import json
import uuid as _uuid
from types import SimpleNamespace

import pytest
from conftest import FakePublisher, install_durable_execution_fakes  # noqa: E402

import meshpipeline.application.pipeline_run as wt  # noqa: E402
import meshpipeline.pipeline.graph as graph_module  # noqa: E402
from meshpipeline.persistence.job_state import TransitionResult  # noqa: E402
from meshpipeline.persistence.models import JobStatus  # noqa: E402

# The approved upload these runs carry.
_GEOMETRY_SOURCE = {'source_id': '33333333-3333-4333-8333-333333333333', 'owner_id': 'owner-1', 'object_key': 'sources/33333333-3333-4333-8333-333333333333', 'sha256': '1cf0557a718c367ab1cb8e34e7a86a191fbc5f1ff9f1b64f4466c8d2784ba4b8', 'size_bytes': 512, 'original_filename': 'input.step', 'suffix_hint': '.step'}


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
        self.final_result: dict | None = None
        self.row = SimpleNamespace(owner_id="dev-user", status=JobStatus.pending, failed_reason=None,
                                   created_at=None, ended_at=None, final_result=None)

    async def get_internal(self, db, job_id): return self.row
    async def get_for_owner(self, db, job_id, owner_id): return self.row
    async def transition(self, db, job_id, target, *, allow=None):
        self.row.status = target
        return TransitionResult.applied
    async def update_current_attempt(self, db, job_id, n): pass
    async def set_final_result(self, db, job_id, final_result):
        self.final_result = final_result
        self.row.final_result = final_result


async def _async_noop(): pass
async def _async_noop_args(*a, **k): pass


def _wire(monkeypatch, final_state: dict):
    rec = SimpleNamespace(repo=FakeRepo(), published=[], model_calls=[])

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

    # TRIPWIRE: any model call during finalization is recorded AND fails the run.
    import meshpipeline.contracts.model_inference as mi
    for name in [n for n in dir(mi) if n.startswith("call_")]:
        def _trip(*a, __n=name, **k):
            rec.model_calls.append(__n)
            raise AssertionError(f"a model was called at the terminal boundary: {__n}")
        monkeypatch.setattr(mi, name, _trip)

    import meshpipeline.application.artifact_uploader as au
    async def fake_upload(db, job_id, ws, *, delivery_attempt=0, execution_generation=0, **_kw):
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
    base = {"reviewer_verdict": "PASS", "executor_success": True, "api_failure": "",
            "outcome_message": "", "retry_count": 0, "engine": "cfmesh",
            "openfoam_workspace": "/tmp/ws-x"}
    base.update(over)
    return base


async def _run(monkeypatch, final_state):
    rec = _wire(monkeypatch, final_state)
    result = await wt._run_async(wt.JobRequest(job_id=JOB_ID))
    closings = [e["text"] for e in rec.published if e["type"] == "closing"]
    return rec, result, (closings[-1] if closings else "")


# No model is invoked to phrase or rewrite the terminal result

async def test_no_model_call_occurs_at_the_terminal_boundary(monkeypatch):
    rec, result, closing = await _run(monkeypatch, _state())
    assert rec.model_calls == []
    assert result["status"] == "succeeded"
    assert closing


# No other agent may author the terminal result

async def test_mutation_builder_authored_terminal_prose_is_discarded(monkeypatch):
    forged = "BUILDER SAYS: your mesh is perfect and the download is ready!"
    rec, result, closing = await _run(monkeypatch, _state(
        reviewer_verdict="FAIL", executor_success=False, outcome_message=forged))
    assert forged not in closing
    assert "did not complete successfully" in closing
    assert rec.repo.final_result["outcome_code"] != "success"


async def test_mutation_reviewer_authored_terminal_prose_is_discarded(monkeypatch):
    prose = "REVIEWER: overall this looks great, ship it."
    rec, result, closing = await _run(monkeypatch, _state(
        reviewer_verdict="FAIL", executor_success=False,
        reviewer_feedback=prose, reviewer_result=prose, outcome_message=prose))
    assert prose not in closing
    assert "did not complete successfully" in closing


async def test_mutation_model_prose_cannot_reconstruct_terminal_truth(monkeypatch):
    rec, _result, closing = await _run(monkeypatch, _state(
        reviewer_verdict="FAIL", executor_success=False,
        outcome_message="Mesh generation completed successfully. Review: passed.",
        request_txt="the model said it succeeded"))
    fr = rec.repo.final_result
    assert fr["status"] == "failed"
    assert fr["executor_success"] is False
    assert fr["required_ready"] is False
    assert "completed successfully" not in closing


# The terminal result must reach the Intake-facing conversation

async def test_no_operator_detail_reaches_the_user_facing_stream(monkeypatch):
    rec, _result, _closing = await _run(monkeypatch, _state(
        reviewer_verdict="FAIL", executor_success=False,
        executor_failed_gate="manifest_valid",
        executor_output="[MANIFEST_VALIDATION_FAILED] /srv/workspaces/j/attempt_1/log.checkMesh"
                        " - rerun with maxNonOrtho 65",
        openfoam_workspace="/srv/workspaces/j/attempt_1",
        geometry_source=_GEOMETRY_SOURCE,
        api_failure="reviewer_evidence_missing"))
    blob = json.dumps(rec.published, default=str)
    for plumbing in ("/srv/workspaces", "MANIFEST_VALIDATION_FAILED", "maxNonOrtho",
                     "secret-customer-part.step", "manifest_valid", "log.checkMesh",
                     "checkpointer", "minio"):
        assert plumbing not in blob, f"operator detail reached the user stream: {plumbing!r}"
    assert rec.published, "the run published nothing at all - the assertion is vacuous"


async def test_the_terminal_result_is_published_to_the_user_facing_surface(monkeypatch):
    rec, _result, closing = await _run(monkeypatch, _state())
    closings = [e for e in rec.published if e["type"] == "closing"]
    assert len(closings) == 1, "exactly one terminal message must reach the user"
    assert closing == closings[0]["text"]
    # …and it is the deterministic render of the PERSISTED record, not something else.
    from meshpipeline.application.final_result import FinalResult, render_message
    assert closing == render_message(FinalResult.from_dict(rec.repo.final_result))


async def test_the_published_message_matches_the_persisted_record_exactly(monkeypatch):
    from meshpipeline.application.final_result import FinalResult, render_message
    rec, _result, closing = await _run(monkeypatch, _state(
        reviewer_verdict="FAIL", executor_success=False))
    stored = rec.repo.final_result
    assert render_message(FinalResult.from_dict(stored)) == closing
    # a second round-trip is still identical (no clock/ordering nondeterminism in the render)
    assert render_message(FinalResult.from_dict(dict(stored))) == closing


def test_an_unknown_final_result_schema_version_fails_safe():
    from meshpipeline.application.final_result import FinalResult
    with pytest.raises(ValueError, match="schema_version"):
        FinalResult.from_dict({"schema_version": 999, "job_id": "j", "owner_id": "o",
                               "status": "succeeded"})


@pytest.fixture(autouse=True)
def _execution_fence(monkeypatch):
    # This suite claims delivery with no Redis. The mirror is stood in for at the two seams
    # production resolves; ownership checks, claim transitions and the transaction stay real.
    from tests.execution_fence_double import install
    return install(monkeypatch)
