# Responsibility: Verify REST, Redis and WebSocket replay report the same terminal result for the same run.
from __future__ import annotations

import json
import uuid

import pytest
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

# The five terminal shapes the spec calls out, as (label, durable status, build kwargs).
_SHAPES = [
    ("success", JobStatus.succeeded, {
        "status": TerminalStatus.succeeded, "executor_success": True, "reviewer_verdict": "PASS",
        "failed_gate": "", "api_failure": "", "required_ready": True,
        "delivered_types": ["mesh_bundle"], "optional_warnings": []}),
    ("optional_artifact_warning", JobStatus.succeeded, {
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
    ("artifact_delivery_failure", JobStatus.failed, {
        "status": TerminalStatus.failed, "executor_success": True, "reviewer_verdict": "PASS",
        "failed_gate": "", "api_failure": "", "required_ready": False,
        "delivered_types": [], "optional_warnings": []}),
]


def _make(job_id: str, kw: dict) -> FinalResult:
    return build_final_result(job_id=job_id, owner_id="owner-1", engine="cfmesh",
                              purpose="external_cfd", dimensionality="3D",
                              approved_snapshot_id="snap-1", attempts=3, attempts_max=5, **kw)


@pytest.fixture(autouse=True)
def _fresh_async_redis_client():
    import meshpipeline.adapters.event_stream.redis as redis_mod
    redis_mod._async_redis = None
    yield
    redis_mod._async_redis = None


@pytest.fixture()
async def SessionLocal():
    try:
        connect_args = {"ssl": True} if provcfg.DB_SSL_REQUIRED else {}
        engine = create_async_engine(provcfg.POSTGRES_DSN, connect_args=connect_args, pool_size=4)
        # Alembic is the only thing that creates this schema - see
        # tests/harness_provisioning.py. `create_all` built tables no migration
        # had produced, so a suite could pass against a schema production never has.
        await hp.reset_schema(provcfg.POSTGRES_DSN)
        await hp.truncate_tables(provcfg.POSTGRES_DSN, "simulation_jobs")
    except Exception as exc:  # noqa: BLE001
        pytest.fail("PostgreSQL not reachable for the terminal-surface agreement proof - it "
                    f"must be PROVISIONED, not skipped ({type(exc).__name__}: {exc})")
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


async def _seed(SessionLocal, status: JobStatus, fr: FinalResult) -> uuid.UUID:
    async with SessionLocal() as s:
        job = SimulationJob(owner_id="owner-1", status=status)
        s.add(job)
        await s.commit()
        jid = job.id
        await repo.set_final_result(s, jid, fr.to_dict())
        await s.commit()
        return jid


def _closing_texts(wire_events: list[str]) -> list[str]:
    out = []
    for raw in wire_events:
        try:
            ev = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if ev.get("type") == "closing":
            out.append(ev.get("text", ""))
    return out


@pytest.mark.parametrize("label,status,kw", _SHAPES, ids=[s[0] for s in _SHAPES])
async def test_rest_redis_and_ws_replay_report_the_same_terminal_result(
        label, status, kw, SessionLocal):
    from meshpipeline.adapters.event_stream.redis import JobPublisher, RedisEventSubscription

    job_id = str(uuid.uuid4())
    fr = _make(job_id, kw)
    expected = render_message(fr)
    jid = await _seed(SessionLocal, status, fr)

 # the run publishes its terminal message through the REAL publisher into REAL Redis
    try:
        JobPublisher(str(jid)).closing(expected)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"Redis not reachable for the agreement proof ({type(exc).__name__}: {exc})")

 # surface 1: REST - exactly what api/v1/simulation.py does with the durable row
    async with SessionLocal() as s:
        row = await repo.get_internal(s, jid)
        stored = await repo.get_final_result(s, jid)
    assert stored is not None, f"{label}: final_result did not persist"
    rest_message = render_message(FinalResult.from_dict(stored))

 # surface 2: Redis durable log - read back through the real subscription
    sub = RedisEventSubscription(str(jid))
    await sub.open()
    try:
        backlog = await sub.backlog()
    finally:
        await sub.close()
    redis_closings = _closing_texts(backlog)
    assert len(redis_closings) == 1, f"{label}: expected one terminal event, got {redis_closings}"

 # surface 3: WS reconnect replay - exactly what ws.py's _terminal_closing does
    async with SessionLocal() as s:
        _j = await repo.get_internal(s, jid)
    ws_message = render_message(FinalResult.from_dict(_j.final_result))

 # they agree with each other, with the durable status, and with the artifact claim
    assert rest_message == expected, f"{label}: REST disagrees"
    assert redis_closings[0] == expected, f"{label}: Redis terminal event disagrees"
    assert ws_message == expected, f"{label}: WS replay disagrees"
    assert row.status == status, f"{label}: durable status changed"
    assert stored["status"] == status.value, f"{label}: record disagrees with SimulationJob.status"

    # artifact readiness must match what the message claims
    if stored["required_ready"]:
        assert "ready to download" in expected, label
    else:
        assert "ready to download" not in expected, f"{label}: claimed a download it cannot serve"

    # a failure shape never sounds like a success on ANY surface
    if status is JobStatus.failed:
        for surface, msg in (("REST", rest_message), ("redis", redis_closings[0]),
                             ("ws", ws_message)):
            assert "did not complete successfully" in msg, f"{label}/{surface}"
            assert "completed successfully" not in msg.replace(
                "did not complete successfully", ""), f"{label}/{surface}"


async def test_the_optional_warning_shape_still_reports_a_ready_deliverable(SessionLocal):
    job_id = str(uuid.uuid4())
    fr = _make(job_id, dict(_SHAPES[1][2]))
    msg = render_message(fr)
    assert fr.status is TerminalStatus.succeeded
    assert "ready to download" in msg
    assert "optional preview could not be prepared" in msg
