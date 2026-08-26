# Responsibility: Verify the persisted payload is what both backends consume, across every patch-tampering case.
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests import harness_provisioning as hp

import meshpipeline.agents.intake.admission_token as at
import meshpipeline.application.pipeline_run as pr
import meshpipeline.settings.providers as provcfg
from meshpipeline.application import dispatch_contract as dc
from meshpipeline.application.approved_patch_contract import ApprovedPatchContract
from meshpipeline.contracts import pipeline_execution as pe
from meshpipeline.persistence.models import SimulationJob
from meshpipeline.persistence.repositories.job_repository import JobRepository

# The approved upload these runs carry.
_GEOMETRY_SOURCE = {'source_id': '33333333-3333-4333-8333-333333333333', 'owner_id': 'owner-1', 'object_key': 'sources/33333333-3333-4333-8333-333333333333', 'sha256': '1cf0557a718c367ab1cb8e34e7a86a191fbc5f1ff9f1b64f4466c8d2784ba4b8', 'size_bytes': 512, 'original_filename': 'input.step', 'suffix_hint': '.step'}
from meshpipeline.contracts.geometry_source import GeometrySourceRef  # noqa: E402

_SOURCE_REF = GeometrySourceRef.from_payload(_GEOMETRY_SOURCE)

# The scale that upload was approved under. Dispatch refuses bytes without one, because a
# run that does not know its own size cannot be meshed.
_GEOMETRY_INTERPRETATION = {
    'interpretation_id': '55555555-5555-4555-8555-555555555555',
    'geometry_source_id': _SOURCE_REF.source_id,
    'unit': 'mm', 'scale_to_metres': 1e-3,
    'basis': 'user_confirmed', 'evidence': 'declared'}


repo = JobRepository()

_PATCHES = [{"name": "inlet", "type": "inlet"}, {"name": "outlet", "type": "outlet"},
            {"name": "wall", "type": "wall"}]
_R = "A complete requirements summary for this duct case. " * 4
_B = "Acceptance criteria for the reviewer of this duct case. " * 3


@pytest.fixture()
async def SessionLocal():
    connect_args = {"ssl": True} if provcfg.DB_SSL_REQUIRED else {}
    try:
        engine = create_async_engine(provcfg.POSTGRES_DSN, connect_args=connect_args, pool_size=6)
        # Alembic is the only thing that creates this schema - see
        # tests/harness_provisioning.py. `create_all` built tables no migration
        # had produced, so a suite could pass against a schema production never has.
        await hp.reset_schema(provcfg.POSTGRES_DSN)
        await hp.truncate_tables(provcfg.POSTGRES_DSN, "simulation_jobs")
    except Exception as exc:  # noqa: BLE001
        pytest.fail("PostgreSQL not reachable for the dispatch-seam runtime test - it must be "
                    f"PROVISIONED, not skipped ({type(exc).__name__}: {exc})")
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


class _CapturingLauncher:
    def __init__(self):
        self.payload = None

    async def launch(self, db, job_id: str, payload: dict) -> None:
        self.payload = payload


def _contract(patches=None, required=None) -> dict:
    return ApprovedPatchContract.build(_PATCHES if patches is None else patches,
                                       required=required).to_dict()


def _approved_payload(job_id: str, snapshot_id: str, *, patches=None, contract=None) -> dict:
    exec_patches = list(_PATCHES if patches is None else patches)
    _engine_params = {"element_order": "2"}
    return dc.build(
        job_id=job_id, owner_id="owner-1", geometry_source=_GEOMETRY_SOURCE,
        geometry_interpretation=_GEOMETRY_INTERPRETATION, session_id=str(uuid.uuid4()),
        request_txt=_R, review_brief_txt=_B, intake_patches=exec_patches, dimensionality="3D",
        purpose="internal_cfd", input_kind="fluid-domain", mesh_engine="gmsh", domain="duct",
        engine_params=_engine_params,
            requested_mesh_fidelity="standard", intake_events=[], approved_snapshot_id=snapshot_id,
        approved_patch_contract=_contract() if contract is None else contract,
        approved_intent_fingerprint=at.fingerprint(at.approved_intent_canonical(
            engine="gmsh", purpose="internal_cfd", input_kind="fluid-domain", dimensionality="3D",
            patches=exec_patches, engine_params=_engine_params,
            requested_mesh_fidelity="standard",
            request_txt=_R, source_ref=_SOURCE_REF)))


async def _seed_job(SessionLocal) -> uuid.UUID:
    async with SessionLocal() as s:
        job = SimulationJob(owner_id="owner-1")
        s.add(job)
        await s.commit()
        return job.id


async def _dispatch_for_real(SessionLocal, job_id, payload) -> dict:
    launcher = _CapturingLauncher()
    prior = pe._launcher
    pe.set_pipeline_launcher(launcher)
    try:
        async with SessionLocal() as db:
            await pr.dispatch(db, str(job_id), payload)
    finally:
        pe._launcher = prior
    return launcher.payload


def _in_fresh_process_like_thread(fn, *args, **kwargs):
    import meshpipeline.persistence.session as _sess

    def _runner():
        _sess.reset_session_state()
        try:
            return fn(*args, **kwargs)
        finally:
            _sess.reset_session_state()
    return asyncio.to_thread(_runner)


def _capture_jobrequest(monkeypatch):
    seen: dict = {}

    async def _fake_run_async(req):
        seen["req"] = req
        return {"status": "succeeded", "stubbed": True}

    monkeypatch.setattr(pr, "_run_async", _fake_run_async)
    return seen


async def test_the_persisted_payload_is_what_both_backends_consume(SessionLocal, monkeypatch):
    job_id = await _seed_job(SessionLocal)
    snapshot_id = uuid.uuid4().hex
    launched = await _dispatch_for_real(SessionLocal, job_id, _approved_payload(str(job_id), snapshot_id))

    # durable evidence: the row the Cloud Run path will reload
    async with SessionLocal() as s:
        persisted = await repo.get_dispatch_payload(s, job_id)
    assert persisted == launched, "the launched payload IS the persisted row"
    assert persisted["schema_version"] == dc.DISPATCH_SCHEMA_VERSION
    assert persisted["approved_snapshot_id"] == snapshot_id

 # backend 1: the REAL Celery task function, called synchronously
    import meshpipeline.adapters.pipeline_execution.celery as celery_adapter
    seen = _capture_jobrequest(monkeypatch)
    out = await asyncio.to_thread(celery_adapter.run_simulation, **dc.to_run_kwargs(persisted))
    assert out["status"] == "succeeded"          # no TypeError - the defect's signature
    celery_req = seen["req"]
    assert celery_req.approved_snapshot_id == snapshot_id
    assert celery_req.intake_patches == _PATCHES and celery_req.request_txt == _R
    assert celery_req.mesh_engine == "gmsh" and celery_req.purpose == "internal_cfd"

 # backend 2: the REAL one-shot reconstruction, given only the job id
    seen2 = _capture_jobrequest(monkeypatch)
    out2 = await _in_fresh_process_like_thread(pr.run_from_job, str(job_id))
    assert out2["status"] == "succeeded"
    recon_req = seen2["req"]
    assert recon_req.approved_snapshot_id == snapshot_id

 # one schema, not two dictionaries
    fields = list(pr.JobRequest.__slots__)
    assert {f: getattr(celery_req, f) for f in fields} == {f: getattr(recon_req, f) for f in fields}

    # the approved canonical payload survived the whole seam unchanged
    assert at.fingerprint(at.canonical_payload(
        recon_req.mesh_engine, recon_req.purpose, recon_req.input_kind,
        recon_req.dimensionality, recon_req.intake_patches, recon_req.engine_params)) \
        == at.fingerprint(at.canonical_payload(
            "gmsh", "internal_cfd", "fluid-domain", "3D", _PATCHES, {"element_order": "2"}))


async def test_a_direct_row_without_the_contract_still_reconstructs(SessionLocal, monkeypatch):
    # A DIRECT dispatch carries no approval provenance and no typed patch contract. That is a
    # current supported shape, not an old one: the envelope version is present and current.
    job_id = await _seed_job(SessionLocal)
    legacy = {"schema_version": dc.DISPATCH_SCHEMA_VERSION,
              "job_id": str(job_id), "owner_id": "owner-1", "mesh_engine": "cfmesh",
              "purpose": "internal_cfd", "input_kind": "fluid-domain", "dimensionality": "3D",
              "intake_patches": list(_PATCHES), "request_txt": _R, "review_brief_txt": _B}
    async with SessionLocal() as s:
        await repo.set_dispatch_payload(s, job_id, legacy)
        await s.commit()

    seen = _capture_jobrequest(monkeypatch)
    out = await _in_fresh_process_like_thread(pr.run_from_job, str(job_id))
    assert out["status"] == "succeeded"
    assert seen["req"].approved_snapshot_id == "", "defaulted deliberately, never invented"
    assert seen["req"].mesh_engine == "cfmesh"


async def test_an_unreadable_future_payload_fails_the_job_clearly(SessionLocal, monkeypatch):
    job_id = await _seed_job(SessionLocal)
    future = _approved_payload(str(job_id), "snap")
    future["schema_version"] = dc.DISPATCH_SCHEMA_VERSION + 1
    async with SessionLocal() as s:
        await repo.set_dispatch_payload(s, job_id, future)
        await s.commit()

    seen = _capture_jobrequest(monkeypatch)
    with pytest.raises(SystemExit, match="schema_version 3 is not"):
        await _in_fresh_process_like_thread(pr.run_from_job, str(job_id))
    assert "req" not in seen, "the run must not start on a payload it cannot read"

    async with SessionLocal() as s:
        row = await repo.get_internal(s, job_id)
    assert row.pipeline_dispatch_state == "launch_failed"


async def test_celery_launch_strips_the_envelope_so_the_worker_task_binds(SessionLocal, monkeypatch):
    import inspect

    import meshpipeline.adapters.pipeline_execution.celery as celery_adapter

    job_id = await _seed_job(SessionLocal)
    payload = _approved_payload(str(job_id), uuid.uuid4().hex)
    assert "schema_version" in payload  # the envelope the launcher must strip

    captured: dict = {}

    def _fake_apply_async(*, kwargs, task_id):
        captured["kwargs"] = kwargs
        captured["task_id"] = task_id
    monkeypatch.setattr(celery_adapter.run_simulation, "apply_async", _fake_apply_async)

    async with SessionLocal() as db:
        await celery_adapter.launch(db, str(job_id), payload)

    assert "schema_version" not in captured["kwargs"], "launch leaked the envelope into task kwargs"
    assert captured["task_id"] == str(job_id)
    # the exact defect signature: the task kwargs must BIND to the run entry
    inspect.signature(pr.run_pipeline).bind(**captured["kwargs"])
    # and they carry the approved run-determining fields
    assert captured["kwargs"]["intake_patches"] == _PATCHES
    assert captured["kwargs"]["approved_patch_contract"]["required"] is True


# the EXACT approved-patch-contract invariant, through the REAL reconstruction path
# Every rejection is proven through run_from_job (the durable Cloud Run reconstruction). A mismatch
# is caught in dispatch_contract.validate - BEFORE run_pipeline builds a JobRequest - so
# _run_async, the graph, the builder, the executor and any native runner are never reached. The
# graph-driver stub (_capture_jobrequest) records whether a JobRequest was ever built; "req" absent
# is the proof that nothing downstream ran.


async def _seed_payload(SessionLocal, payload) -> None:
    job_id = uuid.UUID(payload["job_id"])
    async with SessionLocal() as s:
        await repo.set_dispatch_payload(s, job_id, payload)
        await s.commit()


async def _expect_refused_before_builder(SessionLocal, monkeypatch, payload, *, match):
    seen = _capture_jobrequest(monkeypatch)  # stubs _run_async; records a JobRequest if ever built
    await _seed_payload(SessionLocal, payload)
    with pytest.raises(SystemExit, match=match):
        await _in_fresh_process_like_thread(pr.run_from_job, payload["job_id"])
    assert "req" not in seen, "the run must NOT start - no JobRequest, no builder, no executor, no mesh"
    async with SessionLocal() as s:
        row = await repo.get_internal(s, uuid.UUID(payload["job_id"]))
    assert row.pipeline_dispatch_state == "launch_failed"


async def test_case1_exact_approved_patch_set_passes_admission(SessionLocal, monkeypatch):
    job_id = await _seed_job(SessionLocal)
    payload = _approved_payload(str(job_id), uuid.uuid4().hex)  # exact _PATCHES + matching contract
    assert payload["approved_patch_contract"]["required"] is True
    await _seed_payload(SessionLocal, payload)
    seen = _capture_jobrequest(monkeypatch)
    out = await _in_fresh_process_like_thread(pr.run_from_job, str(job_id))
    assert out["status"] == "succeeded"
    assert seen["req"].intake_patches == _PATCHES
    assert seen["req"].approved_patch_contract["fingerprint"] == _contract()["fingerprint"]


async def test_case2_entire_patch_set_removed_fails_before_builder(SessionLocal, monkeypatch):
    job_id = await _seed_job(SessionLocal)
    p = _approved_payload(str(job_id), uuid.uuid4().hex); p["intake_patches"] = []
    await _expect_refused_before_builder(SessionLocal, monkeypatch, p, match="does not match")


async def test_case3_one_approved_patch_removed_fails_before_builder(SessionLocal, monkeypatch):
    job_id = await _seed_job(SessionLocal)
    p = _approved_payload(str(job_id), uuid.uuid4().hex); p["intake_patches"] = list(_PATCHES[:2])
    await _expect_refused_before_builder(SessionLocal, monkeypatch, p, match="does not match")


async def test_case4_extra_patch_inserted_fails_before_builder(SessionLocal, monkeypatch):
    job_id = await _seed_job(SessionLocal)
    p = _approved_payload(str(job_id), uuid.uuid4().hex)
    p["intake_patches"] = _PATCHES + [{"name": "extra", "type": "wall"}]
    await _expect_refused_before_builder(SessionLocal, monkeypatch, p, match="does not match")


async def test_case5_patch_renamed_fails_before_builder(SessionLocal, monkeypatch):
    job_id = await _seed_job(SessionLocal)
    p = _approved_payload(str(job_id), uuid.uuid4().hex)
    p["intake_patches"] = [{"name": "INLET", "type": "inlet"}, *_PATCHES[1:]]  # renamed
    await _expect_refused_before_builder(SessionLocal, monkeypatch, p, match="does not match")


async def test_case6_patch_role_changed_fails_before_builder(SessionLocal, monkeypatch):
    job_id = await _seed_job(SessionLocal)
    p = _approved_payload(str(job_id), uuid.uuid4().hex)
    p["intake_patches"] = [{"name": "inlet", "type": "wall"}, *_PATCHES[1:]]  # re-roled
    await _expect_refused_before_builder(SessionLocal, monkeypatch, p, match="does not match")


async def test_case7_duplicate_patch_inserted_fails_before_builder(SessionLocal, monkeypatch):
    job_id = await _seed_job(SessionLocal)
    p = _approved_payload(str(job_id), uuid.uuid4().hex)
    p["intake_patches"] = _PATCHES + [{"name": "wall", "type": "wall"}]  # duplicate name
    await _expect_refused_before_builder(SessionLocal, monkeypatch, p, match="duplicate|does not match")


async def test_case8_merged_patches_change_the_set_and_fail_before_builder(SessionLocal, monkeypatch):
    job_id = await _seed_job(SessionLocal)
    p = _approved_payload(str(job_id), uuid.uuid4().hex)
    p["intake_patches"] = [{"name": "inletOutlet", "type": "inlet"}, {"name": "wall", "type": "wall"}]
    await _expect_refused_before_builder(SessionLocal, monkeypatch, p, match="does not match")


async def test_case9_fingerprint_changed_without_changing_patches_fails_reconstruction(SessionLocal, monkeypatch):
    job_id = await _seed_job(SessionLocal)
    p = _approved_payload(str(job_id), uuid.uuid4().hex)
    p["approved_patch_contract"]["fingerprint"] = "0" * 64  # swapped, patches unchanged
    await _expect_refused_before_builder(SessionLocal, monkeypatch, p, match="fingerprint")


async def test_case10_patches_changed_while_old_fingerprint_retained_fails_reconstruction(SessionLocal, monkeypatch):
    job_id = await _seed_job(SessionLocal)
    p = _approved_payload(str(job_id), uuid.uuid4().hex)
    # tamper the contract's OWN patch list but keep its old fingerprint
    p["approved_patch_contract"]["patches"] = p["approved_patch_contract"]["patches"][:2]
    p["intake_patches"] = list(_PATCHES[:2])
    await _expect_refused_before_builder(SessionLocal, monkeypatch, p, match="fingerprint")


async def test_case11_approved_contract_explicitly_empty_passes(SessionLocal, monkeypatch):
    job_id = await _seed_job(SessionLocal)
    p = _approved_payload(str(job_id), uuid.uuid4().hex, patches=[],
                          contract=_contract(patches=[], required=False))
    await _seed_payload(SessionLocal, p)
    seen = _capture_jobrequest(monkeypatch)
    out = await _in_fresh_process_like_thread(pr.run_from_job, str(job_id))
    assert out["status"] == "succeeded"
    assert seen["req"].intake_patches == []
    assert seen["req"].approved_patch_contract["required"] is False


async def test_case12_direct_dispatch_without_a_snapshot_keeps_its_semantics(SessionLocal, monkeypatch):
    job_id = await _seed_job(SessionLocal)
    direct = dc.build(
        job_id=str(job_id), owner_id="owner-1", geometry_source=_GEOMETRY_SOURCE,
        geometry_interpretation=_GEOMETRY_INTERPRETATION,
        session_id=str(uuid.uuid4()), request_txt=_R, review_brief_txt=_B, intake_patches=[],
        dimensionality="3D", purpose="internal_cfd", input_kind="fluid-domain", mesh_engine="gmsh",
        domain="duct", engine_params={}, intake_events=[])
    assert direct.get("approved_snapshot_id", "") == "" and direct.get("approved_patch_contract") is None
    await _seed_payload(SessionLocal, direct)
    seen = _capture_jobrequest(monkeypatch)
    out = await _in_fresh_process_like_thread(pr.run_from_job, str(job_id))
    assert out["status"] == "succeeded"
    assert seen["req"].approved_patch_contract is None


async def test_case13_unambiguous_direct_job_reconstructs(SessionLocal, monkeypatch):
    job_id = await _seed_job(SessionLocal)
    legacy = {"schema_version": dc.DISPATCH_SCHEMA_VERSION,
              "job_id": str(job_id), "owner_id": "owner-1", "mesh_engine": "cfmesh",
              "purpose": "internal_cfd", "input_kind": "fluid-domain", "dimensionality": "3D",
              "intake_patches": [], "request_txt": _R, "review_brief_txt": _B}
    await _seed_payload(SessionLocal, legacy)
    seen = _capture_jobrequest(monkeypatch)
    out = await _in_fresh_process_like_thread(pr.run_from_job, str(job_id))
    assert out["status"] == "succeeded"
    assert seen["req"].approved_patch_contract is None


async def test_case14_approved_snapshot_with_missing_contract_fails_safely(SessionLocal, monkeypatch):
    job_id = await _seed_job(SessionLocal)
    # An approved-snapshot payload that carries an approved_snapshot_id but NO typed
    # approved_patch_contract. The invariant is enforced by the presence of the snapshot id
    # without a typed contract, and it is reached with a current envelope version.
    ambiguous = {"schema_version": dc.DISPATCH_SCHEMA_VERSION,
                 "job_id": str(job_id), "owner_id": "owner-1",
                 "mesh_engine": "cfmesh", "purpose": "internal_cfd", "input_kind": "fluid-domain",
                 "dimensionality": "3D", "intake_patches": list(_PATCHES), "request_txt": _R,
                 "review_brief_txt": _B, "approved_snapshot_id": uuid.uuid4().hex}
    await _expect_refused_before_builder(SessionLocal, monkeypatch, ambiguous,
                                         match="no typed approved_patch_contract")


async def test_the_run_boundary_admission_gate_refuses_without_running_builder_or_executor(
        SessionLocal, monkeypatch):
    from meshpipeline.persistence.models import JobStatus

    job_id = await _seed_job(SessionLocal)
    # transition the job to pending so _run_async can take it (mirrors dispatch persistence)
    async with SessionLocal() as s:
        await s.execute(text("UPDATE simulation_jobs SET status='pending' WHERE id=:i"),
                        {"i": str(job_id)})
        await s.commit()

    # If ANY graph construction happens, the builder/executor/native path was reached - fail hard.
    import meshpipeline.pipeline.graph as graph_mod
    called = {"build_graph": 0}

    def _boom_build_graph(*a, **k):
        called["build_graph"] += 1
        raise AssertionError("build_graph was invoked - the admission gate did not stop the run")
    monkeypatch.setattr(graph_mod, "build_graph", _boom_build_graph)

    # a JobRequest whose execution patches do NOT match its approved contract
    contract = _contract()  # requires exactly _PATCHES
    req = pr.JobRequest(
        job_id=str(job_id), owner_id="owner-1", mesh_engine="gmsh", purpose="internal_cfd",
        input_kind="fluid-domain", dimensionality="3D", intake_patches=list(_PATCHES[:2]),
        approved_snapshot_id=uuid.uuid4().hex, approved_patch_contract=contract)

    out = await pr._run_async(req)

    assert called["build_graph"] == 0, "the graph must never be built on a contract mismatch"
    assert out["status"] == "failed"
    assert out.get("reason") == "patch_contract_mismatch"
    async with SessionLocal() as s:
        row = await repo.get_internal(s, job_id)
    assert row.status == JobStatus.failed, "the durable terminal status must be failed"


async def test_dispatch_refuses_to_persist_or_launch_an_invalid_payload(SessionLocal):
    job_id = await _seed_job(SessionLocal)
    launcher = _CapturingLauncher()
    prior = pe._launcher
    pe.set_pipeline_launcher(launcher)
    try:
        async with SessionLocal() as db:
            with pytest.raises(dc.DispatchContractError, match="does not accept"):
                await pr.dispatch(db, str(job_id), {"schema_version": dc.DISPATCH_SCHEMA_VERSION,
                                                    "job_id": str(job_id), "rogue": 1})
    finally:
        pe._launcher = prior
    assert launcher.payload is None
    async with SessionLocal() as s:
        assert await repo.get_dispatch_payload(s, job_id) is None
