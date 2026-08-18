# Responsibility: Verify retention purges only unreferenced expired sources, exactly once, keeping lineage.
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

import meshpipeline.settings.policy as polcfg
from meshpipeline.application.maintenance.geometry_retention import (
    SOURCE_REQUIRING_STATES,
    TERMINAL_STATES,
    claim_expired_sources,
    finalize_purge,
    purge_expired_geometry_sources,
    retention_cutoff,
)
from meshpipeline.persistence.models import GeometrySource, JobStatus, SimulationJob

pytestmark = pytest.mark.asyncio
OWNER = "retention-owner"


async def _source(db, store, *, age_days: float, owner: str = OWNER, body: bytes = b"geometry"):
    key = f"sources/{uuid.uuid4()}"
    store.upload_file_bytes(object_key=key, data=body) if hasattr(store, "upload_file_bytes") \
        else _put(store, key, body)
    row = GeometrySource(owner_id=owner, original_filename="part.step", suffix_hint=".step",
                         object_key=key, sha256="a" * 64, size_bytes=len(body))
    db.add(row)
    await db.flush()
    await db.execute(text("update geometry_sources set created_at = :ts where id = :i"),
                     {"ts": datetime.now(UTC) - timedelta(days=age_days), "i": row.id})
    await db.commit()
    await db.refresh(row)
    return row


def _put(store, key, body):
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "b"
        p.write_bytes(body)
        store.upload_file(local_path=p, object_key=key, content_type="application/octet-stream")


async def _job(db, source, status: JobStatus):
    j = SimulationJob(owner_id=source.owner_id, status=status, geometry_source_id=source.id)
    db.add(j)
    await db.commit()
    return j


# the policy itself

def test_the_eligible_and_protected_states_are_taken_from_the_state_machine():
    assert SOURCE_REQUIRING_STATES == {JobStatus.pending, JobStatus.queued,
                                       JobStatus.running, JobStatus.pending_review}
    assert TERMINAL_STATES == {JobStatus.succeeded, JobStatus.failed}
    assert SOURCE_REQUIRING_STATES | TERMINAL_STATES == set(JobStatus)
    assert not (SOURCE_REQUIRING_STATES & TERMINAL_STATES)


def test_the_configured_window_is_thirty_days_by_default():
    assert int(polcfg.UPLOAD_RETENTION_DAYS) == 30
    now = datetime.now(UTC)
    assert retention_cutoff(now) == now - timedelta(days=30)


# 1..5 eligibility

async def test_a_young_source_is_retained(db, store):
    src = await _source(db, store, age_days=1)
    await purge_expired_geometry_sources(db, store)
    await db.refresh(src)
    assert src.purged_at is None
    assert store.exists(object_key=src.object_key)


async def test_an_old_unreferenced_source_is_purged(db, store):
    src = await _source(db, store, age_days=45)
    await purge_expired_geometry_sources(db, store)
    await db.refresh(src)
    assert src.purged_at is not None
    assert not store.exists(object_key=src.object_key)
    await db.refresh(src)
    assert src.purged_at is not None


@pytest.mark.parametrize("status", sorted(SOURCE_REQUIRING_STATES, key=lambda s: s.value))
async def test_an_old_source_with_active_work_is_retained(db, store, status):
    src = await _source(db, store, age_days=45)
    await _job(db, src, status)
    await purge_expired_geometry_sources(db, store)
    await db.refresh(src)
    assert src.purged_at is None, f"{status.value} must protect its source"
    assert store.exists(object_key=src.object_key)


@pytest.mark.parametrize("status", sorted(TERMINAL_STATES, key=lambda s: s.value))
async def test_a_terminal_only_source_is_purged(db, store, status):
    src = await _source(db, store, age_days=45)
    await _job(db, src, status)
    await purge_expired_geometry_sources(db, store)
    await db.refresh(src)
    assert src.purged_at is not None
    assert not store.exists(object_key=src.object_key)


# 6..8 product behaviour

async def test_lineage_survives_the_purge(db, store):
    src = await _source(db, store, age_days=45)
    before = (src.sha256, src.size_bytes, src.original_filename, src.suffix_hint, src.created_at)
    await purge_expired_geometry_sources(db, store)
    await db.refresh(src)
    assert (src.sha256, src.size_bytes, src.original_filename, src.suffix_hint,
            src.created_at) == before
    assert src.id is not None


async def test_a_new_run_after_purge_asks_for_re_upload(db, store):
    from meshpipeline.application.geometry_materializer import resolve_row_ref
    from meshpipeline.contracts.geometry_source import GeometrySourceError, GeometrySourceRef
    from meshpipeline.errors import FailureClass

    src = await _source(db, store, age_days=45)
    await purge_expired_geometry_sources(db, store)
    ref = GeometrySourceRef(source_id=str(src.id), owner_id=OWNER, object_key=src.object_key,
                            sha256=src.sha256, size_bytes=src.size_bytes,
                            original_filename=src.original_filename, suffix_hint=".step")
    with pytest.raises(GeometrySourceError) as exc:
        await resolve_row_ref(db, ref)
    assert exc.value.failure_class is FailureClass.USER_INPUT, "expiry is not corruption"
    assert "upload the file again" in str(exc.value).lower()


async def test_the_expiry_message_leaks_nothing(db, store):
    from meshpipeline.application.geometry_materializer import resolve_row_ref
    from meshpipeline.contracts.geometry_source import GeometrySourceError, GeometrySourceRef

    src = await _source(db, store, age_days=45)
    await purge_expired_geometry_sources(db, store)
    ref = GeometrySourceRef(source_id=str(src.id), owner_id=OWNER, object_key=src.object_key,
                            sha256=src.sha256, size_bytes=src.size_bytes,
                            original_filename=src.original_filename, suffix_hint=".step")
    with pytest.raises(GeometrySourceError) as exc:
        await resolve_row_ref(db, ref)
    msg = str(exc.value)
    for secret in (src.object_key, src.sha256, "sources/", "bucket", "minio", "gcs", "provider"):
        assert secret.lower() not in msg.lower()


# 9..14 races

async def test_two_workers_delete_at_most_once(db, store):
    src = await _source(db, store, age_days=45)
    first = await claim_expired_sources(db, claim_id="worker-a")
    await db.commit()
    second = await claim_expired_sources(db, claim_id="worker-b")
    await db.commit()
    # Other tests' rows share this database, so assert about THIS source rather than the batch.
    assert src.id in [r.id for r in first]
    assert src.id not in [r.id for r in second], \
        "a claimed row must not be handed to a second worker"


async def test_a_stale_claimant_cannot_finalize_another_workers_claim(db, store):
    src = await _source(db, store, age_days=45)
    await claim_expired_sources(db, claim_id="worker-a")
    await db.commit()
    assert await finalize_purge(db, src.id, claim_id="worker-b") is False
    await db.commit()
    await db.refresh(src)
    assert src.purged_at is None


async def test_an_expired_claim_becomes_retryable(db, store):
    src = await _source(db, store, age_days=45)
    await claim_expired_sources(db, claim_id="worker-a")
    await db.commit()
    later = datetime.now(UTC) + timedelta(hours=2)
    again = await claim_expired_sources(db, claim_id="worker-b", now=later)
    await db.commit()
    assert src.id in [r.id for r in again], "an abandoned claim must become retryable"


async def test_a_missing_object_is_idempotent_success(db, store):
    src = await _source(db, store, age_days=45)
    store.delete_object(object_key=src.object_key)         # already gone
    out = await purge_expired_geometry_sources(db, store)
    assert out["failed"] == 0
    await db.refresh(src)
    assert src.purged_at is not None


async def test_a_provider_failure_is_retryable_and_says_nothing_publicly(db, store):
    class _Broken:
        def exists(self, *, object_key):
            return True

        def delete_object(self, *, object_key):
            raise RuntimeError("s3: AccessDenied for bucket prod-secrets key " + object_key)

    src = await _source(db, store, age_days=45)
    out = await purge_expired_geometry_sources(db, _Broken())
    assert out["failed"] >= 1 and out["purged"] == 0
    await db.refresh(src)
    assert src.purged_at is None
    assert src.purge_claim_id is None, "the claim must be released so the next run retries"
    await purge_expired_geometry_sources(db, store)
    await db.refresh(src)
    assert src.purged_at is not None, "the released claim must be retried and succeed"


async def test_repeated_runs_are_idempotent(db, store):
    await _source(db, store, age_days=45)
    first = await purge_expired_geometry_sources(db, store)
    second = await purge_expired_geometry_sources(db, store)
    assert first["purged"] >= 1
    assert second["claimed"] == 0 and second["purged"] == 0


# 15..20 blast radius

async def test_deletion_is_exact_key_and_spares_a_neighbour(db, store):
    old = await _source(db, store, age_days=45, body=b"old")
    neighbour_key = old.object_key + "-neighbour"
    _put(store, neighbour_key, b"keep me")
    await purge_expired_geometry_sources(db, store)
    assert not store.exists(object_key=old.object_key)
    assert store.exists(object_key=neighbour_key), "a prefix delete would have taken this"
    store.delete_object(object_key=neighbour_key)


async def test_purging_one_source_leaves_an_unrelated_one_alone(db, store):
    old = await _source(db, store, age_days=45)
    young = await _source(db, store, age_days=2)
    other_owner = await _source(db, store, age_days=45, owner="someone-else")
    await purge_expired_geometry_sources(db, store)
    await db.refresh(old); await db.refresh(young); await db.refresh(other_owner)
    assert old.purged_at is not None
    assert young.purged_at is None and store.exists(object_key=young.object_key)
    # another tenant's OLD source is also eligible on its own merits - but by its own row,
    # never as a side effect of this one
    assert other_owner.purged_at is not None
    assert other_owner.id != old.id


async def test_deleting_a_job_does_not_delete_a_shared_source_row(db, store):
    src = await _source(db, store, age_days=1)
    a = await _job(db, src, JobStatus.succeeded)
    await _job(db, src, JobStatus.succeeded)
    await db.execute(text("delete from simulation_jobs where id = :i"), {"i": a.id})
    await db.commit()
    still = (await db.execute(select(GeometrySource).where(GeometrySource.id == src.id))).scalar_one()
    assert still.purged_at is None and still.object_key == src.object_key
