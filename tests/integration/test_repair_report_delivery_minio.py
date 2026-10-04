# Responsibility: Verify a CAD inspection report is downloadable from a real store for a blocked job as well as a delivered one.
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests import harness_provisioning as hp

import meshpipeline.settings.providers as provcfg
from meshpipeline.application.repair_report_delivery import (
    REPAIR_REPORT_LOGICAL_KEY,
    deliver_repair_report,
)
from meshpipeline.artifact_keys import repair_report_key
from meshpipeline.persistence.models import ArtifactType, SimulationJob

# THE POINT OF THIS TEST. The report has to reach the two readers who need it most, and both of
# them read it on a run that produced NO mesh: the operator deciding whether the file is worth
# repairing, and the customer being told why their geometry was refused. Mesh delivery
# (artifact_uploader) cannot carry it there - it runs only on success, from a workspace - so the
# evidence path is proved separately, against the real store and the migrated schema.

_REPORT = {
    "status": "repairable",
    "report": {"summary": "a wire has a gap",
               "defects": [{"code": "wire_gap", "severity": "error", "count": 2}]},
}


@pytest.fixture()
async def SessionLocal():
    connect_args = {"ssl": True} if provcfg.DB_SSL_REQUIRED else {}
    try:
        engine = create_async_engine(provcfg.POSTGRES_DSN, connect_args=connect_args, pool_size=6)
        # MIGRATION-authoritative: this is also the proof that revision 0011's new
        # `artifacttype` label exists in a schema built the way production builds one.
        await hp.reset_schema(provcfg.POSTGRES_DSN)
    except Exception as exc:  # noqa: BLE001
        pytest.fail("PostgreSQL not reachable for the repair-report test - it must be "
                    f"PROVISIONED, not skipped ({type(exc).__name__}: {exc})")
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture()
def real_store():
    from meshpipeline.adapters.object_storage.minio import MinioStore
    store = MinioStore()
    try:
        store._ensure_bucket(store._client())
        store._client().bucket_exists(store._bucket)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"MinIO not reachable for the repair-report test - PROVISION it ({exc})")
    import meshpipeline.contracts.object_storage as osmod
    prev = osmod._store
    osmod.set_object_store(store)
    yield store
    osmod.set_object_store(prev)


async def _seed_job(SessionLocal) -> uuid.UUID:
    async with SessionLocal() as s:
        job = SimulationJob(owner_id="owner-1")
        s.add(job)
        await s.commit()
        return job.id


async def _artifacts(SessionLocal, job_id):
    from meshpipeline.persistence.repositories.artifact_repository import ArtifactRepository
    async with SessionLocal() as s:
        return await ArtifactRepository().get_by_job(s, job_id)


async def test_a_blocked_job_still_has_a_downloadable_inspection_report(SessionLocal, real_store):
    job_id = await _seed_job(SessionLocal)

    outcome = await deliver_repair_report(
        SessionLocal, job_id=str(job_id), report=_REPORT,
        repair_status="repairable", delivery_attempt=0, execution_generation=1)

    assert outcome == "created"
    rows = await _artifacts(SessionLocal, job_id)
    assert [r.artifact_type for r in rows] == [ArtifactType.repair_report]
    row = rows[0]
    assert row.logical_key == REPAIR_REPORT_LOGICAL_KEY
    assert row.storage_key == repair_report_key(str(job_id))
    assert row.size_bytes > 0

    # DOWNLOADABLE, and carrying the defect the operator has to act on
    fetched = json.loads(real_store.get_bytes(object_key=row.storage_key))
    assert fetched["repair_status"] == "repairable"
    assert fetched["report"]["report"]["defects"][0]["code"] == "wire_gap"
    assert real_store.create_download_url(
        object_key=row.storage_key, expires_in=__import__("datetime").timedelta(minutes=5))


async def test_the_report_alone_never_makes_a_job_look_delivered(SessionLocal, real_store):
    from meshpipeline.application.artifact_policy import optional_warnings, required_ready

    job_id = await _seed_job(SessionLocal)
    await deliver_repair_report(SessionLocal, job_id=str(job_id), report=_REPORT,
                               repair_status="repairable")

    delivered = sorted({r.artifact_type.value for r in await _artifacts(SessionLocal, job_id)})
    assert delivered == ["repair_report"]
    # the readiness policy reads exactly this list; evidence must not satisfy it
    assert required_ready("gmsh", delivered) is False
    assert optional_warnings(delivered) == ["mesh"]


async def test_redelivering_the_same_attempt_leaves_one_unchanged_row(SessionLocal, real_store):
    job_id = await _seed_job(SessionLocal)

    first = await deliver_repair_report(SessionLocal, job_id=str(job_id), report=_REPORT,
                                        repair_status="repairable", delivery_attempt=1,
                                        execution_generation=1)
    second = await deliver_repair_report(SessionLocal, job_id=str(job_id), report=_REPORT,
                                         repair_status="repairable", delivery_attempt=1,
                                         execution_generation=1)

    # BOTH SUCCEED, and the second is not a conflict: inspection reads the immutable uploaded
    # source, so a re-delivery of one attempt is the same evidence, not a competing version of it.
    # The repository reports it as `created` rather than `idempotent` because the compare-and-set
    # permits the write and rewrites the row to identical values - `idempotent` is reserved for a
    # write the CAS BLOCKED. What matters to this service is the state that follows, asserted
    # below: one row, the same bytes, nothing superseded.
    assert (first, second) == ("created", "created")
    rows = await _artifacts(SessionLocal, job_id)
    assert len(rows) == 1
    assert rows[0].delivery_attempt == 1 and rows[0].execution_generation == 1
    assert json.loads(real_store.get_bytes(object_key=rows[0].storage_key))[
        "report"] == _REPORT


async def test_a_newer_generation_supersedes_the_earlier_report(SessionLocal, real_store):
    job_id = await _seed_job(SessionLocal)

    await deliver_repair_report(SessionLocal, job_id=str(job_id), report=_REPORT,
                               delivery_attempt=0, execution_generation=1)
    await deliver_repair_report(SessionLocal, job_id=str(job_id), report=_REPORT,
                               delivery_attempt=0, execution_generation=2)

    rows = await _artifacts(SessionLocal, job_id)
    assert len(rows) == 1
    assert rows[0].execution_generation == 2
