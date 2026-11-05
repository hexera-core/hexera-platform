# Responsibility: Verify a terminal verdict survives a process restart identically.
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests import harness_provisioning as hp

import meshpipeline.settings.providers as provcfg
from meshpipeline.application.final_result import (
    FinalResult,
    TerminalStatus,
    build_final_result,
    render_message,
)
from meshpipeline.persistence.models import JobStatus, SimulationJob
from meshpipeline.persistence.repositories.job_repository import JobRepository

repo = JobRepository()

# (label, JobStatus, build kwargs) - the five terminal shapes the spec calls out.
_SHAPES = [
    ("success", JobStatus.succeeded, {
        "status": TerminalStatus.succeeded, "executor_success": True, "reviewer_verdict": "PASS",
        "failed_gate": "", "api_failure": "", "required_ready": True,
        "delivered_types": ["mesh_bundle"], "optional_warnings": []}),
    ("success_with_optional_warning", JobStatus.succeeded, {
        "status": TerminalStatus.succeeded, "executor_success": True, "reviewer_verdict": "PASS",
        "failed_gate": "", "api_failure": "", "required_ready": True,
        "delivered_types": ["mesh_bundle"], "optional_warnings": ["mesh"]}),
    ("executor_failure", JobStatus.failed, {
        "status": TerminalStatus.failed, "executor_success": False, "reviewer_verdict": "",
        "failed_gate": "", "api_failure": "", "required_ready": False,
        "delivered_types": [], "optional_warnings": []}),
    ("reviewer_rejection", JobStatus.failed, {
        "status": TerminalStatus.failed, "executor_success": True, "reviewer_verdict": "FAIL",
        "failed_gate": "", "api_failure": "", "required_ready": False,
        "delivered_types": [], "optional_warnings": []}),
    ("delivery_failure", JobStatus.failed, {
        "status": TerminalStatus.failed, "executor_success": True, "reviewer_verdict": "PASS",
        "failed_gate": "", "api_failure": "", "required_ready": False,
        "delivered_types": [], "optional_warnings": []}),
]


def _make(kw: dict) -> FinalResult:
    return build_final_result(job_id="seed", owner_id="owner-1", engine="cfmesh",
                              purpose="external_cfd", dimensionality="3D",
                              approved_snapshot_id="snap-1", attempts=3, attempts_max=5, **kw)


def _engine():
    connect_args = {"ssl": True} if provcfg.DB_SSL_REQUIRED else {}
    return create_async_engine(provcfg.POSTGRES_DSN, connect_args=connect_args, pool_size=4)


@pytest.fixture()
async def SessionLocalA():
    try:
        engine = _engine()
        # Alembic is the only thing that creates this schema - see
        # tests/harness_provisioning.py. `create_all` built tables no migration
        # had produced, so a suite could pass against a schema production never has.
        await hp.reset_schema(provcfg.POSTGRES_DSN)
        await hp.truncate_tables(provcfg.POSTGRES_DSN, "simulation_jobs")
    except Exception as exc:  # noqa: BLE001
        pytest.fail("PostgreSQL not reachable for the final-result restart proof - it must be "
                    f"PROVISIONED, not skipped ({type(exc).__name__}: {exc})")
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


async def _seed(SessionLocal, status: JobStatus) -> uuid.UUID:
    async with SessionLocal() as s:
        job = SimulationJob(owner_id="owner-1", status=status)
        s.add(job)
        await s.commit()
        return job.id


async def test_verdict_survives_a_process_restart_identically(SessionLocalA):
    for label, status, kw in _SHAPES:
        fr = _make(kw)
        assert fr.status == TerminalStatus(status.value), label
        job_id = await _seed(SessionLocalA, status)

 # persist the verdict on engine A, then DISPOSE it (process A shuts down)
        async with SessionLocalA() as s:
            await repo.set_final_result(s, job_id, fr.to_dict())
            await s.commit()
    # engine A is disposed by the fixture teardown AFTER the test; simulate the restart with a
    # brand-new engine/connection opened here (nothing shared with A but the durable rows).
    engine_b = _engine()
    try:
        SessionLocalB = async_sessionmaker(bind=engine_b, expire_on_commit=False)
        # reload EVERY seeded job through the fresh process and re-derive its verdict
        async with SessionLocalB() as s:
            rows = (await s.execute(text(
                "select id, status, final_result from simulation_jobs order by created_at"))).all()
        assert len(rows) == len(_SHAPES)
        for (label, _status, kw), (_rid, rstatus, rfr) in zip(_SHAPES, rows, strict=True):
            assert rfr is not None, f"{label}: final_result did not persist"
            # the fresh process renders from the durable row ONLY
            reloaded = FinalResult.from_dict(rfr)
            after = render_message(reloaded)
            # identical to what process A would have shown, and consistent with the durable status
            assert after == render_message(_make(kw)), f"{label}: verdict changed across restart"
            assert reloaded.status.value == rstatus, f"{label}: rendered status disagrees with DB"
            # the two terminal READ surfaces both do exactly this (get_job + WS _terminal_closing)
            api_msg = render_message(FinalResult.from_dict(rfr))
            ws_msg = render_message(FinalResult.from_dict(rfr))
            assert api_msg == ws_msg == after, f"{label}: read surfaces disagree"
            # no infrastructure detail ever entered the durable JSON or the message
            import json as _json
            blob = _json.dumps(rfr) + after
            for bad in ("jobs/", ".tar.gz", "minio", "postgres", "Traceback", "sk-", "://"):
                assert bad.lower() not in blob.lower(), f"{label}: {bad!r} leaked ({blob[:120]})"
    finally:
        await engine_b.dispose()
