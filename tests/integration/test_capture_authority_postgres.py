# Responsibility: Verify that same authority under real concurrency leaves one row and surfaces no uniqueness error.
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from tests import harness_provisioning as hp

if not os.getenv("DATABASE_URL"):
    pytest.skip("a real PostgreSQL endpoint is required", allow_module_level=True)

from meshpipeline.persistence.repositories import capture_repository as cap

OWNER = "owner-capture-tests"


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


async def _new_job(SessionLocal) -> str:
    from meshpipeline.persistence.models import SimulationJob
    row = SimulationJob(owner_id=OWNER)
    async with SessionLocal() as db:
        db.add(row)
        await db.commit()
        return str(row.id)


@pytest.fixture()
async def job(SessionLocal):
    return await _new_job(SessionLocal)


def _rec(job, op_key, payload, *, gen=1, owner=OWNER, name="node_effect"):
    return cap.record_operation(owner_id=owner, job_id=job, execution_generation=gen,
                                op_key=op_key, name=name, payload=payload)


# the five-outcome table

async def test_a_new_operation_is_inserted(job):
    assert _rec(job, "terminal", {"verdict": "PASS"}).outcome == "inserted"
    assert len(cap.trusted_operations(owner_id=OWNER, job_id=job)) == 1


async def test_the_same_operation_with_the_same_content_is_a_no_op(job):
    _rec(job, "terminal", {"verdict": "PASS"})
    r = _rec(job, "terminal", {"verdict": "PASS"})
    assert r.outcome == "duplicate" and r.trusted
    assert len(cap.trusted_operations(owner_id=OWNER, job_id=job)) == 1


async def test_the_same_operation_with_different_content_is_quarantined(job):
    _rec(job, "terminal", {"verdict": "PASS"})
    r = _rec(job, "terminal", {"verdict": "FAIL"})

    assert r.outcome == "conflicted" and not r.trusted
    assert cap.trusted_operations(owner_id=OWNER, job_id=job) == []
    evidence = cap.conflicted_operations(owner_id=OWNER, job_id=job)
    assert len(evidence) == 1
    # the ORIGINAL payload survives as evidence, and the competing digest is recorded beside it
    assert evidence[0]["payload_sha256"] == cap.payload_sha256({"verdict": "PASS"})
    assert evidence[0]["conflict_evidence"][0]["competing_sha256"] == \
        cap.payload_sha256({"verdict": "FAIL"})


async def test_a_conflicted_operation_never_becomes_trusted_again(job):
    _rec(job, "terminal", {"verdict": "PASS"})
    _rec(job, "terminal", {"verdict": "FAIL"})
    # a later replay that happens to match the ORIGINAL content must not restore trust
    r = _rec(job, "terminal", {"verdict": "PASS"})

    assert r.outcome == "already_conflicted" and not r.trusted
    assert cap.trusted_operations(owner_id=OWNER, job_id=job) == []


async def test_distinct_operations_with_identical_content_are_both_kept(job):
    _rec(job, "build:1", {"cells": 1000})
    _rec(job, "build:2", {"cells": 1000})
    assert len(cap.trusted_operations(owner_id=OWNER, job_id=job)) == 2


# isolation

async def test_the_same_operation_in_another_job_is_distinct(SessionLocal, job):
    other = await _new_job(SessionLocal)
    _rec(job, "terminal", {"verdict": "PASS"})
    assert _rec(other, "terminal", {"verdict": "FAIL"}).outcome == "inserted"
    assert len(cap.trusted_operations(owner_id=OWNER, job_id=job)) == 1
    assert len(cap.trusted_operations(owner_id=OWNER, job_id=other)) == 1


async def test_the_same_operation_in_another_generation_is_distinct(job):
    _rec(job, "terminal", {"verdict": "PASS"}, gen=1)
    assert _rec(job, "terminal", {"verdict": "FAIL"}, gen=2).outcome == "inserted"
    assert len(cap.trusted_operations(owner_id=OWNER, job_id=job)) == 2
    assert len(cap.trusted_operations(owner_id=OWNER, job_id=job, execution_generation=1)) == 1


async def test_another_tenant_can_neither_read_nor_conflict_the_record(job):
    _rec(job, "terminal", {"verdict": "PASS"})

    # the other tenant sees nothing, even naming the right job
    assert cap.trusted_operations(owner_id="someone-else", job_id=job) == []
    assert cap.conflicted_operations(owner_id="someone-else", job_id=job) == []

    # and writing the same identity does not touch the first tenant's row
    assert _rec(job, "terminal", {"verdict": "FAIL"}, owner="someone-else").outcome == "inserted"
    mine = cap.trusted_operations(owner_id=OWNER, job_id=job)
    assert len(mine) == 1 and mine[0]["payload"] == {"verdict": "PASS"}




# the real race

_RACER = """
import json, os, sys, time
sys.path.insert(0, os.environ["SRC"])
from meshpipeline.persistence.repositories import capture_repository as cap
job, payload, at = sys.argv[1], json.loads(sys.argv[2]), float(sys.argv[3])
time.sleep(max(0.0, at - time.time()))          # both processes start together
r = cap.record_operation(owner_id="owner-capture-tests", job_id=job, execution_generation=1,
                         op_key="raced", name="node_effect", payload=payload)
print("OUTCOME " + r.outcome)
"""


def _race(job: str, payload_a: dict, payload_b: dict) -> list[str]:
    import time
    start = time.time() + 0.6
    env = {**os.environ, "SRC": os.path.join(os.getcwd(), "src")}
    procs = [subprocess.Popen([sys.executable, "-c", _RACER, job, json.dumps(p), str(start)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
             for p in (payload_a, payload_b)]
    out = []
    for p in procs:
        so, se = p.communicate(timeout=120)
        assert p.returncode == 0, se[-2000:]
        assert "OUTCOME" in so, so
        out.append(so.split("OUTCOME ")[1].strip())
    return out


async def test_two_processes_racing_identical_content_leave_one_trusted_row(job):
    outcomes = _race(job, {"verdict": "PASS"}, {"verdict": "PASS"})

    assert sorted(outcomes) == ["duplicate", "inserted"], outcomes
    rows = cap.trusted_operations(owner_id=OWNER, job_id=job)
    assert len(rows) == 1 and rows[0]["payload"] == {"verdict": "PASS"}
    assert cap.conflicted_operations(owner_id=OWNER, job_id=job) == []


async def test_two_processes_racing_conflicting_content_leave_one_conflicted_operation(job):
    outcomes = _race(job, {"verdict": "PASS"}, {"verdict": "FAIL"})

    # whichever process lost the insert detected the disagreement - the result does not depend on
    # which of them the scheduler ran first
    assert sorted(outcomes) == ["conflicted", "inserted"], outcomes
    assert cap.trusted_operations(owner_id=OWNER, job_id=job) == []
    assert len(cap.conflicted_operations(owner_id=OWNER, job_id=job)) == 1


async def test_the_race_never_surfaces_a_uniqueness_exception(SessionLocal, job):
    for _ in range(3):
        outcomes = _race(await _new_job(SessionLocal), {"v": 1}, {"v": 1})
        assert all(o in ("inserted", "duplicate") for o in outcomes), outcomes
