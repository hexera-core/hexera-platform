# Responsibility: Verify a dispute run routes to review first, rebuilds, and is refused for a purged or unfinished job.
from __future__ import annotations

# pyvista/PIL stand-ins are installed ONCE by tests/unit/conftest.py, and only where the
# real package is genuinely absent. Doing it per-module raced: whichever module was
# imported first decided, and one of them shadowed an installed pyvista.
import json
import uuid as _uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import FakePublisher, install_durable_execution_fakes  # noqa: E402
from langgraph.graph import END

# graph → agents.reviewer.visual → sandbox.sandbox needs a renderer; stub if absent
# (same pattern as test_graph_real.py / test_worker_terminal_matrix.py).
from tests._geometry_support import (
    interpretation_lookup,
    interpretation_ref,
    source_ref,
    source_row,
)
from tests.execution_publisher_double import install as _install_pub

import meshpipeline.settings.runtime as rtcfg
from meshpipeline.persistence.job_state import TransitionResult
from meshpipeline.pipeline.enums import Verdict  # noqa: E402
from meshpipeline.pipeline.graph import route_after_engine_select, route_after_reviewer  # noqa: E402

# The parent run's approved upload; the dispute child inherits it.
_SOURCE = source_ref(owner_id="owner-1", filename="input.step",
                     source_id="22222222-2222-4222-8222-222222222222")
_INTERPRETATION = interpretation_ref(
    geometry_source_id=_SOURCE.source_id,
    interpretation_id="44444444-4444-4444-8444-444444444444")


_DISPUTE = {"of_job_id": "parent-1",
            "flags": [{"x": 0.9, "y": 0.0, "z": 0.1, "note": "layers look collapsed"}],
            "comment": "wing root looks wrong"}


# graph routing  #

def test_normal_run_routes_engine_select_to_geometry_admission():
    # normal run enters the deterministic input gate (geometry admission) before the builder
    assert route_after_engine_select({"user_dispute": {}}) == "node_geometry_admission"


def test_dispute_run_routes_to_reviewer_first():
    assert route_after_engine_select({"user_dispute": _DISPUTE}) == "node_reviewer"


def test_admitted_geometry_routes_to_the_builder():
    from meshpipeline.pipeline.graph import route_after_geometry_admission
    assert route_after_geometry_admission({}) == "node_builder"


def test_rejected_geometry_skips_the_builder_to_the_executor_short_circuit():
    # a measured rejection sets geometry_unsuitable_reason → straight to the executor
    # short-circuit (which reports it) → END; the builder loop is never entered.
    from meshpipeline.pipeline.graph import route_after_geometry_admission
    assert route_after_geometry_admission(
        {"geometry_unsuitable_reason": "[GEOMETRY_UNSUITABLE] self-intersects"}) == "node_executor"


def test_dispute_after_first_review_engine_select_not_rerouted():
    # once a verdict exists (should engine_select ever re-run), take the normal path
    # (geometry admission → builder), not the dispute re-review
    assert route_after_engine_select(
        {"user_dispute": _DISPUTE, "reviewer_verdict": Verdict.FAIL}) == "node_geometry_admission"


def test_dispute_initial_review_always_rebuilds_even_on_pass():
    # user chose rebuild-always: the initial dispute review generates targeted
    # feedback, it does NOT veto the rebuild
    state = {"user_dispute": _DISPUTE, "retry_count": 0,
             "reviewer_verdict": Verdict.PASS, "executor_success": True}
    assert route_after_reviewer(state) == "node_classifier"


def test_dispute_initial_review_rebuilds_on_fail_too():
    state = {"user_dispute": _DISPUTE, "retry_count": 0,
             "reviewer_verdict": Verdict.FAIL, "executor_success": True}
    assert route_after_reviewer(state) == "node_classifier"


def test_dispute_post_rebuild_review_routes_normally():
    # after the rebuild (retry_count >= 1) the normal routing applies: PASS → END
    state = {"user_dispute": _DISPUTE, "retry_count": 1,
             "reviewer_verdict": Verdict.PASS, "executor_success": True}
    assert route_after_reviewer(state) == END


def test_normal_run_reviewer_routing_unchanged():
    assert route_after_reviewer({"user_dispute": {}, "retry_count": 0,
                                 "reviewer_verdict": Verdict.PASS}) == END


# engine preset (dispute)  #

async def test_engine_select_skips_when_engine_preset(monkeypatch, tmp_path):

    monkeypatch.setattr(rtcfg, "CORPUS_DIR", str(tmp_path))
    import meshpipeline.adapters.model_inference.router as r

    async def _never(*a, **k):
        raise AssertionError("no model may be consulted when the engine is preset")
    monkeypatch.setattr(r, "call_classifier_model", _never, raising=False)
    from meshpipeline.pipeline import engine_select as es
    out = await es.node_engine_select({"engine": "snappy", "job_id": "t"})
    assert out == {}


# reviewer context  #

def test_reviewer_context_includes_flagged_regions():
    from meshpipeline.agents.reviewer.context import build_review_prompt
    _, ctx = build_review_prompt(
        manifest={"domain": "external aero", "patch_views": {}},
        nav_context={}, workspace=Path("/tmp/nonexistent"),
        step_basename="crm.step", patch_names=["aircraft"],
        patch_colour_legend="aircraft=red", mesh_units="m",
        request="req", review_brief="brief", job_id="t",
        user_dispute=_DISPUTE,
    )
    assert "ENGINEER-FLAGGED REGIONS" in ctx
    assert "go_to_coordinates(x=0.9" in ctx
    assert "layers look collapsed" in ctx
    assert "wing root looks wrong" in ctx
    assert "original\nacceptance criterion still applies" in ctx.replace(
        "original \nacceptance", "original\nacceptance") or "still applies" in ctx


def test_reviewer_context_comment_only_change_request():
    from meshpipeline.agents.reviewer.context import build_review_prompt
    _, ctx = build_review_prompt(
        manifest={"domain": "external aero", "patch_views": {}},
        nav_context={}, workspace=Path("/tmp/nonexistent"),
        step_basename="crm.step", patch_names=["aircraft"],
        patch_colour_legend="aircraft=red", mesh_units="m",
        request="req", review_brief="brief", job_id="t",
        user_dispute={"of_job_id": "p", "flags": [],
                      "comment": "make the wake region finer"},
    )
    assert "ENGINEER CHANGE REQUEST" in ctx
    assert "make the wake region finer" in ctx
    assert "ENGINEER-FLAGGED REGIONS" not in ctx


def test_reviewer_context_no_dispute_block_on_normal_run():
    from meshpipeline.agents.reviewer.context import build_review_prompt
    _, ctx = build_review_prompt(
        manifest={"domain": "external aero", "patch_views": {}},
        nav_context={}, workspace=Path("/tmp/nonexistent"),
        step_basename="crm.step", patch_names=["aircraft"],
        patch_colour_legend="aircraft=red", mesh_units="m",
        request="req", review_brief="brief", job_id="t",
        user_dispute=None,
    )
    assert "ENGINEER-FLAGGED REGIONS" not in ctx


# worker seeding  #

def _make_parent_workspace(tmp_path: Path, job_id: str, attempt: int = 2) -> Path:
    ws = tmp_path / job_id / "generation_1" / f"attempt_{attempt}"
    ws.mkdir(parents=True)
    (ws / "mesh_manifest.json").write_text(json.dumps(
        {"mesh_mode": "snappy", "patches": {"aircraft": [1]}}))
    (ws / "request.txt").write_text("original CRM request")
    return ws


async def test_worker_seeds_dispute_state_from_parent_workspace(tmp_path, monkeypatch):
    import meshpipeline.application.pipeline_run as wt
    _install_pub(monkeypatch, wt)
    monkeypatch.setattr(rtcfg, "WORKSPACE_BASE", tmp_path)
    parent_id = "parent-job"
    _make_parent_workspace(tmp_path, parent_id, attempt=1)
    ws2 = _make_parent_workspace(tmp_path, parent_id, attempt=2)  # highest wins

    captured: dict = {}

    class FakeGraph:

        async def aget_state(self, config=None):

            # A compiled graph always answers this; the entry asks before deciding fresh vs

            # resume. An empty thread is the right answer for a double that never checkpoints.

            from types import SimpleNamespace

            return SimpleNamespace(next=(), values={}, tasks=(), created_at=None, config={})

        async def ainvoke(self, state, config=None):
            captured.update(state)
            return {**state, "reviewer_verdict": "FAIL", "outcome_message": "done"}

    import meshpipeline.pipeline.graph as graph_module
    monkeypatch.setattr(graph_module, "build_graph", lambda checkpointer=None: FakeGraph())

    import sqlalchemy.ext.asyncio as sa_aio

    async def _noop(): pass
    class _FakeRes:
        def scalars(self): return self
        def all(self): return []
        def scalar_one_or_none(self): return None
        def first(self): return None
    class FakeSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def commit(self): pass
        async def execute(self, *a, **k): return _FakeRes()
    monkeypatch.setattr(sa_aio, "create_async_engine",
                        lambda *a, **k: SimpleNamespace(dispose=_noop))
    monkeypatch.setattr(sa_aio, "async_sessionmaker", lambda **k: FakeSession)

    import meshpipeline.persistence.repositories.job_repository as jr
    class FakeRepo:
        row = SimpleNamespace(owner_id="dev-user", status=None, failed_reason=None, created_at=None, ended_at=None)
        async def get_internal(self, db, job_id): return self.row
        async def get_for_owner(self, db, job_id, owner_id): return self.row
        async def transition(self, db, job_id, target, *, allow=None):
            self.row.status = target
            return TransitionResult.applied
        async def update_current_attempt(self, db, job_id, n): pass
        async def set_final_result(self, db, job_id, fr): pass
    monkeypatch.setattr(jr, "JobRepository", lambda: FakeRepo())
    monkeypatch.setattr(wt, "_pub", lambda job_id, stage="outcome": FakePublisher(job_id, stage))
    monkeypatch.setattr(wt, "_incr_delivery_count", lambda job_id: 1)
    install_durable_execution_fakes(monkeypatch, wt) # durable lease + outbox seams

    dispute = {**_DISPUTE, "of_job_id": parent_id}
    await wt._run_async(wt.JobRequest(job_id=str(_uuid.uuid4()), user_dispute=dispute))

    assert captured["user_dispute"] == dispute
    assert captured["openfoam_workspace"] == str(ws2)          # highest attempt
    assert captured["mesh_manifest"]["mesh_mode"] == "snappy"
    assert captured["engine"] == "snappy"                       # parent's engine pinned
    assert captured["executor_success"] is True                 # parent passed its gates
    assert captured["request_txt"] == "original CRM request"    # recovered from workspace


async def test_worker_dispute_with_purged_parent_fails_as_system_failure(tmp_path, monkeypatch):
    import meshpipeline.application.pipeline_run as wt
    _install_pub(monkeypatch, wt)
    from meshpipeline.errors import SystemFailure
    monkeypatch.setattr(rtcfg, "WORKSPACE_BASE", tmp_path)   # no parent dir exists

    import sqlalchemy.ext.asyncio as sa_aio

    async def _noop(): pass
    class _FakeRes:
        def scalars(self): return self
        def all(self): return []
        def scalar_one_or_none(self): return None
        def first(self): return None
    class FakeSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def commit(self): pass
        async def execute(self, *a, **k): return _FakeRes()
    monkeypatch.setattr(sa_aio, "create_async_engine",
                        lambda *a, **k: SimpleNamespace(dispose=_noop))
    monkeypatch.setattr(sa_aio, "async_sessionmaker", lambda **k: FakeSession)
    import meshpipeline.persistence.repositories.job_repository as jr
    class FakeRepo:
        async def get_internal(self, db, job_id): return None
        async def get_for_owner(self, db, job_id, owner_id): return None
        async def transition(self, db, job_id, target, *, allow=None): return TransitionResult.applied
    monkeypatch.setattr(jr, "JobRepository", lambda: FakeRepo())
    monkeypatch.setattr(wt, "_pub", lambda job_id, stage="outcome": FakePublisher(job_id, stage))
    monkeypatch.setattr(wt, "_incr_delivery_count", lambda job_id: 1)
    install_durable_execution_fakes(monkeypatch, wt) # durable lease + outbox seams
    import meshpipeline.errors as failures
    monkeypatch.setattr(failures, "record_dead_letter", lambda *a, **k: None)

    with pytest.raises(SystemFailure):
        await wt._run_async(wt.JobRequest(
            job_id=str(_uuid.uuid4()),
            user_dispute={**_DISPUTE, "of_job_id": "gone-job"}))


# API endpoint  #

def _client(monkeypatch, parent_job, quota_error: str = ""):
    from contextlib import asynccontextmanager

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import meshpipeline.api.v1.simulation as sim
    import meshpipeline.persistence.session as dbs
    from meshpipeline.api import security as auth

    monkeypatch.setattr(auth, "verify_identity", lambda k, u, s: "user-1")

    # The dispute inherits the parent's scale, so the endpoint resolves it through the
    # tenant-scoped repository exactly as it resolves the source.
    from meshpipeline.persistence.repositories.geometry_interpretation_repository import (
        GeometryInterpretationRepository as _GIR,
    )
    _interp_patch = interpretation_lookup(_INTERPRETATION, owner_id="user-1")
    monkeypatch.setattr(_GIR, "get_for_owner", _interp_patch.new)

    class FakeDB:
        async def commit(self): pass

        async def execute(self, *_a, **_k):
            # the dispute guard reads the durable viewer payload; no artifact exists in this test.
            # `first()` answers the dispute-operation claim: no prior operation in this suite, so
            # every request here is a first one. Duplicate resolution is proven against real
            # PostgreSQL in tests/integration/test_dispute_idempotency_postgres.py, because a fake
            # cannot enforce the unique index or the advisory lock that make it true.
            from types import SimpleNamespace
            return SimpleNamespace(scalar_one_or_none=lambda: None, first=lambda: None)

    @asynccontextmanager
    async def fake_db():
        yield FakeDB()
    monkeypatch.setattr(dbs, "get_db", fake_db)
    monkeypatch.setattr(sim, "get_db", fake_db)

    async def fake_get_job(db, job_id, owner_id):
        return parent_job
    monkeypatch.setattr(sim.svc, "get_job", fake_get_job)

    async def fake_quota(db, owner_id, *, plan=""):
        if quota_error:
            raise ValueError(quota_error)
    monkeypatch.setattr(sim.svc, "check_quotas", fake_quota)

    import meshpipeline.persistence.repositories.job_repository as jr
    new_job = SimpleNamespace(id=_uuid.uuid4(), geometry_source_id=None,
                              geometry_interpretation_id=None)
    class FakeJobRepo:
        async def create(self, db, owner_id):
            return new_job
    monkeypatch.setattr(jr, "JobRepository", FakeJobRepo)

    import meshpipeline.persistence.repositories.session_repository as sr
    class FakeSessionRepo:
        async def get_by_job_id(self, db, job_id):
            return SimpleNamespace(id=_uuid.uuid4(), review_brief_txt="brief",
                                   intake_patches=[{"name": "aircraft", "type": "wall"}],
                                   dimensionality="3D", purpose="external_cfd",
                                   input_kind="body-surface")
    monkeypatch.setattr(sr, "SessionRepository", FakeSessionRepo)

    # dispatch now goes through the execution seam (which persists the run snapshot on
    # the job record, then launches the backend). Capture the payload at the seam - the
    # same dict that becomes dispatch_payload and drives run_simulation.
    dispatched: dict = {}
    import meshpipeline.application.pipeline_run as _ex
    async def _capture(db, job_id, payload):
        dispatched.update(payload)
    monkeypatch.setattr(_ex, "dispatch", _capture)

    app = FastAPI()
    from meshpipeline.api.v1.simulation import router
    app.include_router(router, prefix="/api/v1/simulation")
    return TestClient(app), dispatched, new_job


def _succeeded_job():
    from meshpipeline.persistence.models import JobStatus
    return SimpleNamespace(owner_id="dev-user", id=_uuid.uuid4(), status=JobStatus.succeeded,
                           workspace_purged=False, geometry_source_id=_SOURCE.source_id,
                           geometry_source=source_row(_SOURCE),
                           # a succeeded job ran at a known scale, and a dispute inherits it
                           geometry_interpretation_id=_INTERPRETATION.interpretation_id)


def test_dispute_endpoint_dispatches_with_parent_context(monkeypatch):
    parent = _succeeded_job()
    client, dispatched, new_job = _client(monkeypatch, parent)
    r = client.post(f"/api/v1/simulation/{parent.id}/dispute",
                    headers={"X-User-Id": "user-1"},
                    json={"flags": [{"x": 1.0, "y": 2.0, "z": 3.0, "note": "gap here"}],
                          "comment": "please check"})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["dispute_of"] == str(parent.id)
    assert body["flags"] == 1
    assert dispatched["user_dispute"]["of_job_id"] == str(parent.id)
    assert dispatched["user_dispute"]["flags"][0]["note"] == "gap here"
    assert dispatched["intake_patches"] == [{"name": "aircraft", "type": "wall"}]
    assert dispatched["geometry_source"]["source_id"] == parent.geometry_source_id


def test_dispute_endpoint_rejects_non_succeeded_job(monkeypatch):
    from meshpipeline.persistence.models import JobStatus
    parent = _succeeded_job()
    parent.status = JobStatus.failed
    client, dispatched, _ = _client(monkeypatch, parent)
    r = client.post(f"/api/v1/simulation/{parent.id}/dispute",
                    headers={"X-User-Id": "user-1"},
                    json={"flags": [{"x": 0, "y": 0, "z": 0}]})
    assert r.status_code == 409
    assert not dispatched


def test_dispute_endpoint_rejects_purged_workspace(monkeypatch):
    parent = _succeeded_job()
    parent.workspace_purged = True
    client, dispatched, _ = _client(monkeypatch, parent)
    r = client.post(f"/api/v1/simulation/{parent.id}/dispute",
                    headers={"X-User-Id": "user-1"},
                    json={"flags": [{"x": 0, "y": 0, "z": 0}]})
    assert r.status_code == 409
    assert not dispatched


def test_dispute_endpoint_rejects_empty_dispute_and_unknown_job(monkeypatch):
    client, dispatched, _ = _client(monkeypatch, None)   # get_job → None
    jid = _uuid.uuid4()
    # neither flags nor a change-request comment → nothing to act on
    r = client.post(f"/api/v1/simulation/{jid}/dispute",
                    headers={"X-User-Id": "user-1"},
                    json={"flags": [], "comment": "   "})
    assert r.status_code == 422
    r = client.post(f"/api/v1/simulation/{jid}/dispute",
                    headers={"X-User-Id": "user-1"},
                    json={"flags": [{"x": 0, "y": 0, "z": 0}]})
    assert r.status_code == 404
    assert not dispatched


def test_dispute_endpoint_accepts_comment_only_change_request(monkeypatch):
    # a dispute can be a plain change of heart - no flagged cells required
    parent = _succeeded_job()
    client, dispatched, _ = _client(monkeypatch, parent)
    r = client.post(f"/api/v1/simulation/{parent.id}/dispute",
                    headers={"X-User-Id": "user-1"},
                    json={"comment": "I want finer resolution in the wake region"})
    assert r.status_code == 202, r.text
    assert r.json()["flags"] == 0
    assert dispatched["user_dispute"]["flags"] == []
    assert "wake region" in dispatched["user_dispute"]["comment"]


def test_dispute_endpoint_quota_gated(monkeypatch):
    parent = _succeeded_job()
    client, dispatched, _ = _client(monkeypatch, parent, quota_error="quota exceeded")
    r = client.post(f"/api/v1/simulation/{parent.id}/dispute",
                    headers={"X-User-Id": "user-1"},
                    json={"flags": [{"x": 0, "y": 0, "z": 0}]})
    assert r.status_code == 429
    assert not dispatched


@pytest.fixture(autouse=True)
def _execution_fence(monkeypatch):
    # This suite claims delivery with no Redis. The mirror is stood in for at the two seams
    # production resolves; ownership checks, claim transitions and the transaction stay real.
    from tests.execution_fence_double import install
    return install(monkeypatch)
