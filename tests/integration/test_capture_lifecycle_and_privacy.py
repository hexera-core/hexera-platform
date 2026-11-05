# Responsibility: Verify capture records follow their job's lifecycle, stay tenant-scoped, and redact before storage.
from __future__ import annotations

import json
import os
import uuid

import pytest
from tests import harness_provisioning as hp

if not os.getenv("DATABASE_URL"):
    pytest.skip("a real PostgreSQL endpoint is required", allow_module_level=True)

from tests.product_modes import set_modes

from meshpipeline.persistence.repositories import capture_repository as cap

OWNER = "owner-lifecycle"
STRANGER = "owner-stranger"

#: Generated values shaped like the things that must never leak. None are real.
SECRETS = {
    "api_token": "sk-TESTONLY0123456789abcdefGHIJ",
    "access_key": "AKIAIOSFODNN7EXAMPLE",
    "bucket_object": "sources/11111111-1111-4111-8111-111111111111",
    "local_path": "/srv/workspaces/job-42/geometry/source.step",
    "arn": "arn:aws:s3:::example-bucket/private/key",
    "provider_detail": "anthropic.messages error 529 overloaded",
}


@pytest.fixture()
async def SessionLocal():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import meshpipeline.settings.providers as provcfg

    engine = create_async_engine(provcfg.POSTGRES_DSN, pool_size=8)
    # Alembic is the only thing that creates this schema - see
    # tests/harness_provisioning.py. `create_all` built tables no migration
    # had produced, so a suite could pass against a schema production never has.
    await hp.reset_schema(provcfg.POSTGRES_DSN)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


async def _job(SessionLocal, owner: str = OWNER) -> str:
    from meshpipeline.persistence.models import SimulationJob
    row = SimulationJob(owner_id=owner)
    async with SessionLocal() as db:
        db.add(row)
        await db.commit()
        return str(row.id)


async def _count(SessionLocal, job_id: str) -> int:
    from sqlalchemy import text
    async with SessionLocal() as db:
        r = await db.execute(text("select count(*) from capture_operations where job_id = :j"),
                             {"j": job_id})
        return r.scalar_one()


def _seed(job_id: str, owner: str = OWNER) -> None:
    cap.record_operation(owner_id=owner, job_id=job_id, execution_generation=1,
                         op_key="trusted", name="executor_run", payload={"success": True})
    for payload in ({"verdict": "PASS"}, {"verdict": "FAIL"}):     # becomes conflicted
        cap.record_operation(owner_id=owner, job_id=job_id, execution_generation=1,
                             op_key="disputed", name="final_result_built", payload=payload)


# retention

async def test_deleting_a_job_removes_its_capture_operations(SessionLocal):
    from sqlalchemy import text
    job_id = await _job(SessionLocal)
    _seed(job_id)
    assert await _count(SessionLocal, job_id) == 2                # one trusted, one conflicted

    async with SessionLocal() as db:
        await db.execute(text("delete from simulation_jobs where id = :j"), {"j": job_id})
        await db.commit()

    assert await _count(SessionLocal, job_id) == 0, "capture rows outlived their job"


async def test_conflicted_operations_follow_the_same_lifecycle_as_trusted_ones(SessionLocal):
    from sqlalchemy import text
    job_id = await _job(SessionLocal)
    _seed(job_id)
    assert len(cap.conflicted_operations(owner_id=OWNER, job_id=job_id)) == 1

    async with SessionLocal() as db:
        await db.execute(text("delete from simulation_jobs where id = :j"), {"j": job_id})
        await db.commit()

    assert cap.conflicted_operations(owner_id=OWNER, job_id=job_id) == []
    assert await _count(SessionLocal, job_id) == 0


async def test_deleting_one_owners_job_leaves_another_owners_capture_intact(SessionLocal):
    from sqlalchemy import text
    mine, theirs = await _job(SessionLocal), await _job(SessionLocal, STRANGER)
    _seed(mine)
    _seed(theirs, STRANGER)

    async with SessionLocal() as db:
        await db.execute(text("delete from simulation_jobs where id = :j"), {"j": mine})
        await db.commit()

    assert await _count(SessionLocal, mine) == 0
    assert await _count(SessionLocal, theirs) == 2
    assert len(cap.trusted_operations(owner_id=STRANGER, job_id=theirs)) == 1


async def test_an_export_cannot_resurrect_deleted_records(SessionLocal):
    from sqlalchemy import text

    from meshpipeline.capture.events import EventLog
    job_id = await _job(SessionLocal)
    _seed(job_id)
    assert EventLog(job_id, owner_id=OWNER).load()

    async with SessionLocal() as db:
        await db.execute(text("delete from simulation_jobs where id = :j"), {"j": job_id})
        await db.commit()

    assert EventLog(job_id, owner_id=OWNER).load() == []


async def test_no_orphan_capture_row_can_be_created(SessionLocal):
    import psycopg
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        cap.record_operation(owner_id=OWNER, job_id=str(uuid.uuid4()), execution_generation=1,
                             op_key="orphan", name="x", payload={})


# privacy

async def test_secret_shaped_values_are_redacted_before_they_reach_postgresql(SessionLocal,
                                                                              monkeypatch,
                                                                              tmp_path):
    import meshpipeline.settings.runtime as rtcfg
    from meshpipeline.capture import scope, trace

    job_id = await _job(SessionLocal)
    monkeypatch.setenv("MY_PROVIDER_KEY", SECRETS["api_token"])
    monkeypatch.setattr(rtcfg, "JOBS_DIR", str(tmp_path), raising=False)
    set_modes(monkeypatch, collection=True)

    with scope.capture_scope(OWNER, job_id):
        trace.add_event(job_id, "executor_run",
                        {"stderr": f"failed with {SECRETS['api_token']} while reading "
                                   f"{SECRETS['local_path']}"},
                        attributes={"op_id": "executor:0"})

    stored = json.dumps(cap.trusted_operations(owner_id=OWNER, job_id=job_id), default=str)
    assert SECRETS["api_token"] not in stored, "a raw token reached durable storage"
    assert "REDACTED" in stored


async def test_a_public_event_backlog_carries_no_capture_identity(SessionLocal):
    if not os.getenv("REDIS_URL"):
        pytest.skip("a real Redis endpoint is required")
    from meshpipeline.adapters._shared.redis_client import sync_client
    from meshpipeline.adapters.event_stream.redis import JobPublisher
    from meshpipeline.events.channels import log_key_for

    job_id = await _job(SessionLocal)
    _seed(job_id)
    pub = JobPublisher(job_id, agent="executor")
    pub.stage(op_id="check:0")
    pub.note("Checking the mesh", op_id="check:0")

    backlog = json.dumps([json.loads(x) for x in
                          sync_client().lrange(log_key_for(job_id), 0, -1)])
    rows = cap.trusted_operations(owner_id=OWNER, job_id=job_id)
    for leaked in [r["op_key"] for r in rows] + [r["payload_sha256"] for r in rows]:
        assert leaked not in backlog, "the public stream exposed private capture identity"
    for value in SECRETS.values():
        assert value not in backlog


async def test_a_foreign_tenant_cannot_read_conflict_evidence_either(SessionLocal):
    job_id = await _job(SessionLocal)
    _seed(job_id)
    assert cap.conflicted_operations(owner_id=OWNER, job_id=job_id)
    assert cap.conflicted_operations(owner_id=STRANGER, job_id=job_id) == []
    assert cap.trusted_operations(owner_id=STRANGER, job_id=job_id) == []
