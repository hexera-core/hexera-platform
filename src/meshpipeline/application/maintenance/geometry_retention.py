# Responsibility: Delete uploaded geometry bytes on schedule while keeping the row as lineage.
# Owns: the retention cutoff, the purge claim, finalisation, and claim release.
# Boundaries: it deletes objects, never rows: a terminal result stays readable after the bytes are gone.
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, exists, or_, select, update

import meshpipeline.settings.policy as polcfg
from meshpipeline.persistence.models import GeometrySource, JobStatus, SimulationJob

logger = logging.getLogger(__name__)

#: Job states that still NEED the uploaded bytes. Taken from JobStatus itself rather than matched
#: by name: `pending` and `queued` have not fetched the source yet, `running` may re-materialise
#: after a resume, and `pending_review` can be disputed into a rebuild that re-meshes the SAME
#: upload. Only `succeeded` and `failed` are terminal, and a terminal job needs its result and
#: transcript, not the bytes it was built from.
SOURCE_REQUIRING_STATES: frozenset[JobStatus] = frozenset({
    JobStatus.pending,
    JobStatus.queued,
    JobStatus.running,
    JobStatus.pending_review,
})
TERMINAL_STATES: frozenset[JobStatus] = frozenset(set(JobStatus) - set(SOURCE_REQUIRING_STATES))

#: How long a purge claim may be held before another worker may take it. Long enough for a slow
#: provider delete, short enough that an abandoned claim does not pin the row forever.
CLAIM_EXPIRY = timedelta(minutes=15)


def retention_cutoff(now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) - timedelta(days=int(polcfg.UPLOAD_RETENTION_DAYS))


def _protected_by_active_work():
    return exists().where(and_(
        SimulationJob.geometry_source_id == GeometrySource.id,
        SimulationJob.status.in_(sorted(s.value for s in SOURCE_REQUIRING_STATES)),
    ))


def _eligible_where(cutoff: datetime, now: datetime):
    claim_dead = or_(GeometrySource.purge_claim_id.is_(None),
                     GeometrySource.purge_claimed_at.is_(None),
                     GeometrySource.purge_claimed_at < now - CLAIM_EXPIRY)
    return and_(
        GeometrySource.created_at < cutoff,          # past the retention window
        GeometrySource.purged_at.is_(None),          # not already purged (idempotent)
        claim_dead,                                  # not claimed by a live worker
        ~_protected_by_active_work(),                # no job that still needs the bytes
    )


async def claim_expired_sources(db, *, claim_id: str, limit: int = 50,
                                now: datetime | None = None) -> list[GeometrySource]:
    now = now or datetime.now(UTC)
    cutoff = retention_cutoff(now)
    picked = (select(GeometrySource.id)
              .where(_eligible_where(cutoff, now))
              .order_by(GeometrySource.created_at)
              .limit(limit)
              .with_for_update(skip_locked=True))
    stmt = (update(GeometrySource)
            .where(GeometrySource.id.in_(picked))
            .values(purge_claim_id=claim_id, purge_claimed_at=now)
            .returning(GeometrySource))
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


async def finalize_purge(db, source_id, *, claim_id: str,
                         now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    stmt = (update(GeometrySource)
            .where(and_(GeometrySource.id == source_id,
                        GeometrySource.purge_claim_id == claim_id,
                        GeometrySource.purged_at.is_(None)))
            .values(purged_at=now, purge_claim_id=None, purge_claimed_at=None))
    return (await db.execute(stmt)).rowcount == 1


async def release_claim(db, source_id, *, claim_id: str) -> bool:
    stmt = (update(GeometrySource)
            .where(and_(GeometrySource.id == source_id,
                        GeometrySource.purge_claim_id == claim_id))
            .values(purge_claim_id=None, purge_claimed_at=None))
    return (await db.execute(stmt)).rowcount == 1


async def purge_expired_geometry_sources(db, store, *, limit: int = 50,
                                         now: datetime | None = None) -> dict:
    claim_id = uuid.uuid4().hex
    claimed = await claim_expired_sources(db, claim_id=claim_id, limit=limit, now=now)
    await db.commit()

    purged, failed, already_absent = 0, 0, 0
    for row in claimed:
        key, source_id = row.object_key, row.id
        try:
            if hasattr(store, "exists") and not store.exists(object_key=key):
                already_absent += 1
            else:
                store.delete_object(object_key=key)
        except Exception as exc:                     # noqa: BLE001 - provider detail stays internal
            failed += 1
            # The provider's message is an OPERATOR fact. It names buckets, endpoints and
            # sometimes credentials, so it is logged and never returned.
            logger.warning("geometry retention: provider delete failed for source %s: %s",
                           source_id, exc)
            await release_claim(db, source_id, claim_id=claim_id)
            await db.commit()
            continue
        if await finalize_purge(db, source_id, claim_id=claim_id, now=now):
            purged += 1
        await db.commit()

    return {"claimed": len(claimed), "purged": purged, "already_absent": already_absent,
            "failed": failed, "retention_days": int(polcfg.UPLOAD_RETENTION_DAYS)}
