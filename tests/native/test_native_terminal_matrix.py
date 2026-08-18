# Responsibility: Verify a real native mesh agrees on every durable surface, and survives restart, replay and staleness.
from __future__ import annotations

import asyncio
import hashlib
import uuid
from pathlib import Path

import _terminal_support as sup
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests import harness_provisioning as hp

import meshpipeline.settings.providers as provcfg
from meshpipeline.application import pipeline_run as pr
from meshpipeline.application.dispatch_contract import build as dc_build
from meshpipeline.application.dispatch_contract import to_run_kwargs
from meshpipeline.application.final_result import FinalResult, render_message
from meshpipeline.persistence.models import FailedReason, JobStatus, SimulationJob
from meshpipeline.persistence.repositories.artifact_repository import ArtifactRepository
from meshpipeline.persistence.repositories.job_repository import JobRepository

pytestmark = pytest.mark.native_terminal

ENGINES = ["cfmesh", "snappy", "gmsh", "snappy_multiregion", "vmtk"]
OWNER = "owner-terminal"
repo = JobRepository()
arepo = ArtifactRepository()


def _sha256(p) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _reset_process_caches():
    import meshpipeline.adapters.event_stream.redis as _redis
    import meshpipeline.persistence.session as _sess
    _sess._engine = None
    _sess._session_factory = None
    _redis._async_redis = None


@pytest.fixture(scope="session", autouse=True)
def _migrated_schema():
    import asyncio

    from tests import harness_provisioning as _hp

    import meshpipeline.settings.providers as _prov
    revision = asyncio.run(asyncio.to_thread(_hp.upgrade_to_head, _prov.POSTGRES_DSN))
    assert revision, "the native tier's database was not migrated to head"
    yield revision


@pytest.fixture(scope="session", autouse=True)
def _adapters():
    import meshpipeline.contracts.mesh_execution as meshmod
    import meshpipeline.contracts.object_storage as osmod
    from meshpipeline.adapters.mesh_execution.local import LocalMeshExecutor
    from meshpipeline.runtime.composition import install_adapters
    install_adapters()
    assert osmod.get_object_store() is not None
    # This tier validates the mesh IMAGE, so it meshes in-process. The application never does:
    # install_adapters() binds the Cloud Run executor, and the tier replaces that binding here.
    meshmod.set_mesh_executor(LocalMeshExecutor())
    yield
    meshmod.set_mesh_executor(None)


async def _seed_pending_job(SL) -> str:
    async with SL() as s:
        job = SimulationJob(owner_id=OWNER, status=JobStatus.pending)
        s.add(job)
        await s.commit()
        return str(job.id)


def _job_request(kwargs: dict) -> pr.JobRequest:
    keep = ("job_id", "owner_id", "geometry_source", "session_id", "request_txt", "review_brief_txt",
            "intake_patches", "dimensionality", "purpose", "input_kind", "user_dispute",
            "mesh_engine", "domain", "engine_params", "approved_snapshot_id",
            "patch_contract_required", "approved_patch_contract")
    return pr.JobRequest(**{k: v for k, v in kwargs.items() if k in keep})


async def _drive_terminal(SL, job_id: str, engine: str, native: dict, monkeypatch,
                          *, reviewer="PASS"):
    payload = dc_build(job_id=job_id, owner_id=OWNER, mesh_engine=engine,
                       purpose="", domain=native["domain"])
    async with SL() as s:
        await repo.set_dispatch_payload(s, uuid.UUID(job_id), payload)
        await s.commit()
        reloaded = await repo.get_dispatch_payload(s, uuid.UUID(job_id))
    kwargs = to_run_kwargs(reloaded, where="native-terminal reload")
    req = _job_request(kwargs)
    monkeypatch.setattr("meshpipeline.pipeline.graph.build_graph",
                        sup.build_stub_graph_factory(native, reviewer_verdict=reviewer))
    return await pr._run_async(req)


async def _read_surfaces(SL, job_id: str) -> dict:
    from meshpipeline.adapters.event_stream.redis import RedisEventSubscription
    async with SL() as s:
        # Read as the OWNER, through the tenant-scoped accessor the API itself uses - not
        # `get_internal`. A terminal surface the owner cannot read is not a delivered surface,
        # so scoping belongs in the assertion rather than being bypassed for convenience.
        row = await repo.get_for_owner(s, uuid.UUID(job_id), OWNER)
        stored_fr = await repo.get_final_result(s, uuid.UUID(job_id))
        artifacts = await arepo.get_by_job(s, uuid.UUID(job_id))
    rest_msg = render_message(FinalResult.from_dict(stored_fr)) if stored_fr else None
    ws_msg = render_message(FinalResult.from_dict(row.final_result)) if row.final_result else None
    sub = RedisEventSubscription(job_id)
    await sub.open()
    try:
        backlog = await sub.backlog()
    finally:
        await sub.close()
    import json as _json
    closings = [_json.loads(r).get("text", "") for r in backlog
                if _json.loads(r).get("type") == "closing"]
    return {"status": row.status, "final_result": stored_fr, "rest_msg": rest_msg,
            "ws_msg": ws_msg, "redis_closings": closings,
            "artifacts": [{"type": a.artifact_type.value, "logical_key": a.logical_key,
                           "storage_key": a.storage_key, "checksum": a.checksum,
                           "size": a.size_bytes} for a in artifacts]}


# five-engine native-to-terminal agreement
@pytest.mark.parametrize("engine", ENGINES)
def test_native_to_terminal_agreement(engine, tmp_path, monkeypatch, canonical_provenance):
    SL, dbengine = _reset_and_session()

    async def run():
        # reset the DB so each engine's job is isolated
        # The schema comes from Alembic, once per session (see `_migrated_schema`); this only
        # clears rows so each engine's job is isolated. `create_all` here built tables no
        # migration had produced and left `alembic_version` empty.
        await hp.truncate_tables(provcfg.POSTGRES_DSN, "simulation_jobs")
        native = sup.author_and_mesh(engine, tmp_path / engine)
        assert native["native_rc"] == 0 and native["gates_ok"], native
        native_marker_sha = _sha256(Path(native["workspace"]) / native["marker"])

        job_id = await _seed_pending_job(SL)
        result = await _drive_terminal(SL, job_id, engine, native, monkeypatch)
        assert result["status"] == "succeeded", result

        surf = await _read_surfaces(SL, job_id)
        # status + final_result schema/engine
        assert surf["status"] == JobStatus.succeeded
        fr = FinalResult.from_dict(surf["final_result"])
        assert surf["final_result"]["schema_version"] >= 1
        assert fr.engine == engine, f"final_result engine {fr.engine} != {engine}"
        # the message names THIS engine and no OTHER (skip names that are substrings of this
        # engine's own name, e.g. 'snappy' ⊂ 'snappy_multiregion', which are not cross-references)
        for other in ENGINES:
            if other != engine and other not in engine and engine not in other:
                assert other not in (surf["rest_msg"] or ""), f"{engine} message names {other}"
        assert fr.status.value == "succeeded"
        # required artifact ready + byte-match to the native output
        bundle = next((a for a in surf["artifacts"] if a["type"] == "mesh_bundle"), None)
        assert bundle is not None, "no required mesh_bundle delivered"
        import meshpipeline.contracts.object_storage as osmod
        dest = tmp_path / f"{engine}_fetched.tar.gz"
        osmod.get_object_store().download_file(object_key=bundle["storage_key"], destination=dest)
        import tarfile
        with tarfile.open(dest) as tf:
            mm = [m for m in tf.getnames() if m.endswith(Path(native["marker"]).name)]
            assert mm, f"delivered bundle missing marker {native['marker']}"
            fb = tf.extractfile(mm[0]).read()
        assert hashlib.sha256(fb).hexdigest() == native_marker_sha, "bundle marker != native output"
        # REST == Redis == WS == persisted, all say succeeded, exactly one terminal event
        assert surf["rest_msg"] == surf["ws_msg"]
        assert len(surf["redis_closings"]) == 1, surf["redis_closings"]
        assert surf["redis_closings"][0] == surf["rest_msg"]
        assert "ready to download" in surf["rest_msg"]
        return {"engine": engine, "job_id": job_id, "final_result": surf["final_result"],
                "artifacts": surf["artifacts"], "native_marker_sha256": native_marker_sha,
                "rest_msg": surf["rest_msg"]}

    evidence = asyncio.run(run())
    _dump_evidence(f"terminal_{engine}", evidence)
    asyncio.run(dbengine.dispose())


# restart / replay per engine (no repeated side effects)
@pytest.mark.parametrize("engine", ["cfmesh", "vmtk"])
def test_restart_replay_no_repeat(engine, tmp_path, monkeypatch, canonical_provenance):
    SL, dbengine = _reset_and_session()
    calls = {"native": 0, "upload": 0}
    _real_dispatch = pr_run_engine_ref()
    import meshpipeline.application.artifact_uploader as au
    _real_upload = au.upload_job_artifacts

    async def _count_upload(*a, **k):
        calls["upload"] += 1
        return await _real_upload(*a, **k)
    monkeypatch.setattr(au, "upload_job_artifacts", _count_upload)

    async def run():
        # The schema comes from Alembic, once per session (see `_migrated_schema`); this only
        # clears rows so each engine's job is isolated. `create_all` here built tables no
        # migration had produced and left `alembic_version` empty.
        await hp.truncate_tables(provcfg.POSTGRES_DSN, "simulation_jobs")
        native = sup.author_and_mesh(engine, tmp_path / engine)
        calls["native"] += 1                       # author_and_mesh ran the native engine ONCE
        assert native["gates_ok"]
        job_id = await _seed_pending_job(SL)
        r1 = await _drive_terminal(SL, job_id, engine, native, monkeypatch)
        assert r1["status"] == "succeeded"
        surf1 = await _read_surfaces(SL, job_id)
        # exactly one delivery pass; exactly one REQUIRED bundle (an engine may also ship an OPTIONAL
        # .msh preview row - that is not a duplicate of the required deliverable)
        _bundles1 = [a for a in surf1["artifacts"] if a["type"] == "mesh_bundle"]
        assert calls["upload"] == 1 and len(_bundles1) == 1, surf1["artifacts"]

 # simulate an API/worker restart: drop in-memory caches, then a RE-DELIVERY of the job
        _reset_process_caches()
        req = _job_request(to_run_kwargs(
            await _reload_payload(SL, job_id), where="restart"))
        monkeypatch.setattr("meshpipeline.pipeline.graph.build_graph",
                            sup.build_stub_graph_factory(native))
        r2 = await pr._run_async(req)          # re-delivered → must be skipped, no side effects
        assert r2.get("skipped") == "already_terminal", r2
        assert calls["upload"] == 1, "artifact delivery repeated after restart"

 # fresh-process reads reconstruct the SAME durable result
        _reset_process_caches()
        surf2 = await _read_surfaces(SL, job_id)
        assert surf2["status"] == JobStatus.succeeded
        assert surf2["final_result"] == surf1["final_result"]
        _bundles2 = [a for a in surf2["artifacts"] if a["type"] == "mesh_bundle"]
        assert len(_bundles2) == 1, "duplicate required-bundle Artifact row after restart"
        assert len(surf2["artifacts"]) == len(surf1["artifacts"]), "artifact rows changed on restart"
        assert _bundles2[0]["checksum"] == _bundles1[0]["checksum"]
        assert len(surf2["redis_closings"]) == 1, "second terminal message synthesized"
        return {"engine": engine, "job_id": job_id, "native_calls": calls["native"],
                "upload_calls": calls["upload"], "artifact_rows": len(surf2["artifacts"]),
                "bundle_rows": len(_bundles2)}

    evidence = asyncio.run(run())
    _dump_evidence(f"restart_{engine}", evidence)
    asyncio.run(dbengine.dispose())


async def _reload_payload(SL, job_id):
    async with SL() as s:
        return await repo.get_dispatch_payload(s, uuid.UUID(job_id))


# native terminal failure containment
@pytest.mark.parametrize("engine", ["cfmesh", "gmsh"])
def test_native_gate_failure_is_terminal_failed(engine, tmp_path, monkeypatch, canonical_provenance):
    SL, dbengine = _reset_and_session()

    async def run():
        # The schema comes from Alembic, once per session (see `_migrated_schema`); this only
        # clears rows so each engine's job is isolated. `create_all` here built tables no
        # migration had produced and left `alembic_version` empty.
        await hp.truncate_tables(provcfg.POSTGRES_DSN, "simulation_jobs")
        native = sup.author_and_mesh(engine, tmp_path / engine)
        assert native["gates_ok"], "precondition: a clean native pass before we corrupt it"
        # remove the manifest → manifest_valid gate fails on re-evaluation (evidence corruption
        # OUTSIDE production source; the engine algorithm is untouched)
        (Path(native["workspace"]) / "mesh_manifest.json").unlink()
        from meshpipeline.engines.gates import GateCtx, run_gates
        from meshpipeline.engines.registry import get_spec
        ctx = GateCtx(workspace=Path(native["workspace"]), engine=engine, domain=native["domain"],
                      intake_patches=native["contract"], engine_params={})
        ok, failed, _ = run_gates(get_spec(engine).gates, ctx, on_result=lambda *a: None)
        assert not ok, "corrupting evidence must fail the real gate chain"
        native2 = dict(native, gates_ok=False, failed_gate=failed)

        job_id = await _seed_pending_job(SL)
        r = await _drive_terminal(SL, job_id, engine, native2, monkeypatch)
        assert r["status"] == "failed", r
        surf = await _read_surfaces(SL, job_id)
        assert surf["status"] == JobStatus.failed
        assert not any(a["type"] == "mesh_bundle" for a in surf["artifacts"]), "no artifact on gate fail"
        assert "did not complete successfully" in surf["rest_msg"]
        assert surf["rest_msg"] == surf["ws_msg"] == surf["redis_closings"][0]
        return {"engine": engine, "failed_gate": failed, "status": "failed"}

    evidence = asyncio.run(run())
    _dump_evidence(f"gatefail_{engine}", evidence)
    asyncio.run(dbengine.dispose())


def test_native_success_but_artifact_delivery_failure(tmp_path, monkeypatch, canonical_provenance):
    engine = "cfmesh"
    SL, dbengine = _reset_and_session()

    async def run():
        # The schema comes from Alembic, once per session (see `_migrated_schema`); this only
        # clears rows so each engine's job is isolated. `create_all` here built tables no
        # migration had produced and left `alembic_version` empty.
        await hp.truncate_tables(provcfg.POSTGRES_DSN, "simulation_jobs")
        native = sup.author_and_mesh(engine, tmp_path / engine)
        assert native["gates_ok"]
        # make the REQUIRED upload fail deterministically (store raises) - real native success behind it
        import meshpipeline.application.artifact_uploader as au
        from meshpipeline.application.artifact_uploader import RequiredArtifactDeliveryError

        async def _boom(*a, **k):
            raise RequiredArtifactDeliveryError("injected MinIO outage")
        monkeypatch.setattr(au, "upload_job_artifacts", _boom)

        job_id = await _seed_pending_job(SL)
        r = await _drive_terminal(SL, job_id, engine, native, monkeypatch)
        assert r["status"] == "failed", r
        surf = await _read_surfaces(SL, job_id)
        assert surf["status"] == JobStatus.failed
        assert not any(a["type"] == "mesh_bundle" for a in surf["artifacts"])
        fr = surf["final_result"]
        assert fr["status"] == "failed" and not fr["required_ready"]
        assert "did not complete successfully" in surf["rest_msg"]
        assert "completed successfully" not in surf["rest_msg"].replace("did not complete successfully", "")
        # restart preserves failed
        _reset_process_caches()
        surf2 = await _read_surfaces(SL, job_id)
        assert surf2["status"] == JobStatus.failed and surf2["final_result"] == fr
        return {"engine": engine, "status": "failed", "required_ready": fr["required_ready"]}

    evidence = asyncio.run(run())
    _dump_evidence("deliveryfail_cfmesh", evidence)
    asyncio.run(dbengine.dispose())


def test_rejected_terminal_cas_stale_worker_cannot_publish_success(tmp_path, monkeypatch,
                                                                    canonical_provenance):
    engine = "gmsh"
    SL, dbengine = _reset_and_session()

    async def run():
        # The schema comes from Alembic, once per session (see `_migrated_schema`); this only
        # clears rows so each engine's job is isolated. `create_all` here built tables no
        # migration had produced and left `alembic_version` empty.
        await hp.truncate_tables(provcfg.POSTGRES_DSN, "simulation_jobs")
        native = sup.author_and_mesh(engine, tmp_path / engine)
        assert native["gates_ok"]
        job_id = await _seed_pending_job(SL)

        # A competing winner finalizes the job to FAILED after the graph runs but before the stale
        # worker's terminal CAS - injected via a stub graph that finalizes, then returns success-y.
        # The winner must finalize the way a REAL winner does: the atomic terminal transaction
        # (status CAS + final_result + outbox intent) followed by outbox delivery. A bare
        # repo.transition() leaves status=failed with NO final_result and NO closing, so the
        # surfaces this test reads are empty for a reason that has nothing to do with the stale
        # worker - the assertions below then measure the fixture, not the fence.
        from meshpipeline.application import outbox_publisher as _obp
        from meshpipeline.application import terminal_finalize as _tf
        from meshpipeline.application.final_result import TerminalStatus, build_final_result
        from meshpipeline.persistence.job_state import TransitionResult

        winner_fr = build_final_result(
            job_id=job_id, owner_id=OWNER, status=TerminalStatus.failed, engine=engine,
            purpose="external_cfd", dimensionality="3D", approved_snapshot_id="",
            executor_success=True, reviewer_verdict="FAIL", failed_gate="", api_failure="",
            attempts=1, attempts_max=5, required_ready=False, delivered_types=[],
            optional_warnings=[])
        winner_msg = render_message(winner_fr)

        class _RacingStub(sup.StubGraph):
            async def ainvoke(self, initial_state, config=None):
                st = await super().ainvoke(initial_state, config)
                # competing terminal winner: a reaper finalizes running→failed, atomically
                eng2 = create_async_engine(provcfg.POSTGRES_DSN, pool_size=2)
                SL2 = async_sessionmaker(bind=eng2, expire_on_commit=False)
                async with SL2() as s2:
                    out = await _tf.finalize_terminal_atomic(
                        s2, ownership=None, intended_status=JobStatus.failed,
                        failed_reason=FailedReason.reviewer_rejected,
                        final_result_dict=winner_fr.to_dict(), closing_message=winner_msg)
                    await s2.commit()
                assert not out.fenced, "the reaper holds no lease and must not be fenced"
                assert out.transition == TransitionResult.applied, \
                    "reaper should win the CAS from running"
                assert out.enqueued, "the winner must enqueue exactly one terminal closing"
                await _obp.publish_pending(SL2, job_id=uuid.UUID(job_id))
                await eng2.dispose()
                return st
        monkeypatch.setattr("meshpipeline.pipeline.graph.build_graph",
                            lambda *a, **k: _RacingStub(native))
        payload = dc_build(job_id=job_id, owner_id=OWNER, mesh_engine=engine, domain=native["domain"])
        async with SL() as s:
            await repo.set_dispatch_payload(s, uuid.UUID(job_id), payload); await s.commit()
        req = _job_request(to_run_kwargs(await _reload_payload(SL, job_id)))
        r = await pr._run_async(req)

        surf = await _read_surfaces(SL, job_id)
        assert surf["status"] == JobStatus.failed, "stale worker overwrote the winner's terminal state"
        assert "did not complete successfully" in surf["rest_msg"]
        assert "completed successfully" not in surf["rest_msg"].replace("did not complete successfully", "")
        # no duplicate ready mesh_bundle, and exactly one terminal message
        bundles = [a for a in surf["artifacts"] if a["type"] == "mesh_bundle"]
        assert len(bundles) <= 1, "duplicate ready artifact after lost CAS"
        assert len(surf["redis_closings"]) == 1, "a contradictory second terminal message appeared"
        assert surf["redis_closings"][0] == surf["rest_msg"]
        return {"engine": engine, "durable_status": "failed",
                "stale_worker_result": r.get("status"), "mesh_bundles": len(bundles)}

    evidence = asyncio.run(run())
    _dump_evidence("caslost_gmsh", evidence)
    asyncio.run(dbengine.dispose())


# helpers
def _reset_and_session():
    _reset_process_caches()
    engine = create_async_engine(provcfg.POSTGRES_DSN, pool_size=4)
    return async_sessionmaker(bind=engine, expire_on_commit=False), engine


def pr_run_engine_ref():
    from meshpipeline.engines import dispatch
    return dispatch.run_engine_local


def _dump_evidence(name: str, data: dict):
    import json
    import os
    out = os.environ.get("FM_TERMINAL_EVIDENCE_DIR", "")
    if out:
        p = Path(out); p.mkdir(parents=True, exist_ok=True)
        (p / f"{name}.json").write_text(json.dumps(data, indent=2, default=str))
