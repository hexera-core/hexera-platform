# Responsibility: Verify the capture authority inserts once, no-ops an identical replay, quarantines a conflicting one.
from __future__ import annotations

import os
import uuid

import pytest
from tests import harness_provisioning as hp

if not os.getenv("DATABASE_URL"):
    pytest.skip("a real PostgreSQL endpoint is required", allow_module_level=True)

from tests.capture_authority import InMemoryAuthority

OWNER = "owner-contract"


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


async def _job(SessionLocal) -> str:
    from meshpipeline.persistence.models import SimulationJob
    row = SimulationJob(owner_id=OWNER)
    async with SessionLocal() as db:
        db.add(row)
        await db.commit()
        return str(row.id)


@pytest.fixture(params=["postgres", "in_memory"])
async def authority(request, SessionLocal):
    if request.param == "postgres":
        from meshpipeline.persistence.repositories import capture_repository as cap
        yield cap, await _job(SessionLocal)
    else:
        yield InMemoryAuthority(), str(uuid.uuid4())


def _rec(auth, job, op_key, payload, *, gen=1, owner=OWNER, name="node_effect"):
    return auth.record_operation(owner_id=owner, job_id=job, execution_generation=gen,
                                 op_key=op_key, name=name, payload=payload)


async def test_a_new_operation_is_inserted(authority):
    auth, job = authority
    assert _rec(auth, job, "op", {"v": 1}).outcome == "inserted"
    assert len(auth.trusted_operations(owner_id=OWNER, job_id=job)) == 1


async def test_an_identical_replay_is_a_no_op(authority):
    auth, job = authority
    _rec(auth, job, "op", {"v": 1})
    r = _rec(auth, job, "op", {"v": 1})
    assert r.outcome == "duplicate" and r.trusted
    assert len(auth.trusted_operations(owner_id=OWNER, job_id=job)) == 1


async def test_a_conflicting_replay_quarantines_the_operation(authority):
    auth, job = authority
    _rec(auth, job, "op", {"v": 1})
    r = _rec(auth, job, "op", {"v": 2})

    assert r.outcome == "conflicted" and not r.trusted
    assert auth.trusted_operations(owner_id=OWNER, job_id=job) == []
    ev = auth.conflicted_operations(owner_id=OWNER, job_id=job)
    assert len(ev) == 1 and ev[0]["conflict_evidence"][0]["competing_sha256"]


async def test_a_quarantined_operation_never_regains_trust(authority):
    auth, job = authority
    _rec(auth, job, "op", {"v": 1})
    _rec(auth, job, "op", {"v": 2})
    assert _rec(auth, job, "op", {"v": 1}).outcome == "already_conflicted"
    assert auth.trusted_operations(owner_id=OWNER, job_id=job) == []


async def test_distinct_operations_with_identical_content_are_both_kept(authority):
    auth, job = authority
    _rec(auth, job, "a", {"v": 1})
    _rec(auth, job, "b", {"v": 1})
    assert len(auth.trusted_operations(owner_id=OWNER, job_id=job)) == 2


async def test_another_generation_is_a_distinct_operation(authority):
    auth, job = authority
    _rec(auth, job, "op", {"v": 1}, gen=1)
    assert _rec(auth, job, "op", {"v": 2}, gen=2).outcome == "inserted"
    assert len(auth.trusted_operations(owner_id=OWNER, job_id=job)) == 2
    assert len(auth.trusted_operations(owner_id=OWNER, job_id=job,
                                       execution_generation=1)) == 1


async def test_another_tenant_sees_and_touches_nothing(authority):
    auth, job = authority
    _rec(auth, job, "op", {"v": 1})
    assert auth.trusted_operations(owner_id="stranger", job_id=job) == []
    assert auth.conflicted_operations(owner_id="stranger", job_id=job) == []
    assert _rec(auth, job, "op", {"v": 99}, owner="stranger").outcome == "inserted"
    mine = auth.trusted_operations(owner_id=OWNER, job_id=job)
    assert len(mine) == 1 and mine[0]["payload"] == {"v": 1}


async def test_ordering_is_by_the_durable_sequence(authority):
    auth, job = authority
    for i in range(5):
        _rec(auth, job, f"op-{i}", {"i": i})
    rows = auth.trusted_operations(owner_id=OWNER, job_id=job)
    assert [r["payload"]["i"] for r in rows] == [0, 1, 2, 3, 4]
    assert [r["seq"] for r in rows] == sorted(r["seq"] for r in rows)


async def test_two_consecutive_reads_of_unchanged_state_are_identical(authority):
    auth, job = authority
    for i in range(4):
        _rec(auth, job, f"op-{i}", {"i": i})
    assert auth.trusted_operations(owner_id=OWNER, job_id=job) == \
        auth.trusted_operations(owner_id=OWNER, job_id=job)
