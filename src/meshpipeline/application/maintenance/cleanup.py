# Responsibility: Reclaim what finished runs leave behind: workspaces, event logs and stalled jobs.
# Boundaries: bounded, resumable sweeps; each is safe to interrupt and safe to run twice.
import logging
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import meshpipeline.settings.policy as polcfg
import meshpipeline.settings.providers as provcfg
import meshpipeline.settings.runtime as rtcfg

logger = logging.getLogger(__name__)


def _publish_terminal_log(job_id: str) -> None:
    try:
        from meshpipeline.contracts.event_stream import publisher
        from meshpipeline.persistence.repositories.terminal_outbox_repository import (
            dedup_key_for,
        )
        # same terminal identity: a cleanup retry must not add a second ending
        publisher(job_id).closing(
            "This job stopped unexpectedly (the worker did not finish) and has been "
            "marked failed. Please resubmit.", dedup_key_for(job_id))
    except Exception as exc:
        logger.warning("reap_stalled_jobs: could not publish terminal log for %s: %s", job_id, exc)



def purge_expired_workspaces() -> dict:
    import asyncio
    return asyncio.run(_purge_async())


async def _purge_async() -> dict:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from meshpipeline.persistence.models import JobStatus, SimulationJob

    _engine = create_async_engine(
        provcfg.POSTGRES_DSN,
        echo=False,
        pool_size=2,
        max_overflow=2,
        pool_pre_ping=True,
    )
    _SessionLocal = async_sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )

    cutoff = datetime.now(UTC) - timedelta(hours=polcfg.FAILED_JOB_RETENTION_HOURS)
    purged = 0

    try:
        async with _SessionLocal() as db:
            result = await db.execute(
                select(SimulationJob)
                .where(SimulationJob.status == JobStatus.failed)
                .where(SimulationJob.workspace_purged == False)
                .where(SimulationJob.ended_at < cutoff)
            )
            jobs = result.scalars().all()

            training_dir = Path(rtcfg.CORPUS_DIR)
            _pending_job_ids: set[str] = set()
            if training_dir.exists():
                for _sample_dir in training_dir.iterdir():
                    if not _sample_dir.is_dir():
                        continue
                    _jid_file = _sample_dir / ".job_id"
                    if _jid_file.exists() and not (_sample_dir / ".export_complete").exists():
                        try:
                            _pending_job_ids.add(_jid_file.read_text().strip())
                        except OSError:
                            pass

            for job in jobs:
                workspace = rtcfg.WORKSPACE_BASE / str(job.id)
                try:
                    if workspace.exists():
                        export_pending = str(job.id) in _pending_job_ids
                        if export_pending:
                            logger.warning(
                                "Cleanup: skipping workspace %s - export pending (no .export_complete sentinel)",
                                workspace,
                            )
                            continue
                        shutil.rmtree(workspace)
                        logger.info("Purged workspace %s", workspace)

                except OSError as exc:
                    logger.warning("Could not purge %s: %s", workspace, exc)

                job.workspace_purged = True
                purged += 1

            await db.commit()

        logger.info("Cleanup complete - purged %d workspaces", purged)
    finally:
        await _engine.dispose()

    return {"purged": purged}


def _stalled(cutoff):
    """Which jobs count as stalled. Extracted so it can be READ BY A TEST.

    It was inline in the query, which meant the only way to check it was to stand up a database -
    so the lease clause below could be wrong, or absent, and nothing would say so. A condition that
    decides whether a customer's jobs are reaped is worth being able to assert about.
    """
    from sqlalchemy import or_

    from meshpipeline.persistence.models import JobStatus, SimulationJob

    return or_(
        # abandoned mid-run (worker crash)
        (SimulationJob.status == JobStatus.running)
        & SimulationJob.started_at.isnot(None)
        & (SimulationJob.started_at < cutoff),
        # AND THE LEASE ALREADY SAYS SO, HOURS EARLIER. A running job is owned by a worker holding a
        # lease it heartbeats well inside WORKER_LEASE_SECONDS, so an EXPIRED lease means no worker
        # is holding it - that is what the lease is for, and it is known within about fifteen
        # minutes. Waiting STALLED_JOB_TIMEOUT_HOURS on top leaves dead jobs counting against
        # MAX_JOBS_PER_OWNER for the rest of the afternoon: measured here, five jobs killed by a
        # redeploy filled the cap and refused every upload for hours, while the reaper reported
        # "reaped 0 stalled job(s)" throughout.
        #
        # Only where a lease was actually taken. A null `lease_expires_at` is a job nothing ever
        # leased, and reading that as expired would reap the innocent.
        (SimulationJob.status == JobStatus.running)
        & SimulationJob.lease_expires_at.isnot(None)
        & (SimulationJob.lease_expires_at < datetime.now(UTC)),
        # never picked up (started_at is NULL for these)
        SimulationJob.status.in_([JobStatus.pending, JobStatus.queued])
        & (SimulationJob.created_at < cutoff),
    )



def _aware_lt(when) -> bool:
    """Is this timestamp already past? True for a naive one read as UTC, which is what the row holds
    on a database that was not asked for timezones."""
    if when is None:
        return False
    stamp = when if when.tzinfo is not None else when.replace(tzinfo=UTC)
    return stamp < datetime.now(UTC)


def reap_stalled_jobs() -> dict:
    import asyncio
    return asyncio.run(_reap_stalled_async())


async def _reap_stalled_async() -> dict:
    from sqlalchemy import select, update
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from meshpipeline.persistence.models import FailedReason, JobStatus, SimulationJob

    _engine = create_async_engine(provcfg.POSTGRES_DSN, echo=False, pool_size=2,
                                  max_overflow=2, pool_pre_ping=True)
    _SessionLocal = async_sessionmaker(bind=_engine, class_=AsyncSession,
                                       expire_on_commit=False, autocommit=False, autoflush=False)
    cutoff = datetime.now(UTC) - timedelta(hours=polcfg.STALLED_JOB_TIMEOUT_HOURS)
    reaped: list[str] = []
    try:
        async with _SessionLocal() as db:
            rows = (await db.execute(
                select(SimulationJob).where(_stalled(cutoff))
            )).scalars().all()
            now = datetime.now(UTC)
            for job in rows:
                _was = job.status
                # COMPARE-AND-SET on the exact stalled source states: a job that raced to a
                # terminal result between the SELECT above and here is never overwritten by the
                # reaper. The failure reason is stamped only when the reaper actually failed it.
                res = await db.execute(
                    update(SimulationJob).where(
                        SimulationJob.id == job.id,
                        SimulationJob.status.in_([JobStatus.running, JobStatus.pending, JobStatus.queued]),
                    ).values(
                        status=JobStatus.failed,
                        failed_reason=FailedReason.unhandled,
                        ended_at=now,
                        updated_at=now,
                    )
                )
                if res.rowcount == 0:
                    logger.info("reap_stalled_jobs: job %s reached terminal before reaping - left as-is", job.id)
                    continue
                reaped.append(str(job.id))
                # THE LOG READS THE LEASE DEFENSIVELY, AND THE ORDER OF THESE TWO LINES IS WHY.
                #
                # The row is already marked failed by the time this runs, so anything that raises here
                # aborts the loop AFTER a write and every remaining stalled job goes unreaped for
                # another cycle. I added `lease_expires_at` to this line with the lease-aware reaper
                # and read it as a plain attribute: `tests/unit/infra/test_failure_handling_wiring.py`
                # drives the reaper with a stand-in job object, which does not carry it, and the
                # reaper died in its own logging with an AttributeError. It passes at 33ef6be^ and
                # fails at 33ef6be, so this is mine and not pre-existing.
                #
                # A logging line may not be the thing that decides whether a job gets reaped.
                _lease = getattr(job, "lease_expires_at", None)
                logger.warning(
                    "reap_stalled_jobs: job %s stalled in %s (started=%s created=%s lease_expired=%s) - "
                    "marked failed(unhandled)",
                    job.id, _was, job.started_at, job.created_at,
                    _lease is not None and _aware_lt(_lease),
                )
                # Publish a TERMINAL log line so any live WebSocket client
                # streaming this (crash-dropped) job receives a closing message and
                # disconnects, instead of hanging forever waiting for output the
                # dead worker will never produce.
                _publish_terminal_log(str(job.id))
            await db.commit()
    finally:
        await _engine.dispose()
    logger.info("reap_stalled_jobs: reaped %d stalled job(s)", len(reaped))
    return {"reaped": reaped, "count": len(reaped)}



def purge_expired_geometry_sources() -> dict:
    import asyncio
    return asyncio.run(_purge_geometry_sources_async())


async def _purge_geometry_sources_async() -> dict:
    from meshpipeline.application.maintenance.geometry_retention import (
        purge_expired_geometry_sources as _purge,
    )
    from meshpipeline.contracts.object_storage import get_object_store
    from meshpipeline.persistence.session import dispose_engine, get_db

    store = get_object_store()
    if store is None:
        logger.warning("geometry retention: no object store configured - nothing to purge")
        return {"claimed": 0, "purged": 0, "already_absent": 0, "failed": 0}
    try:
        async with get_db() as db:
            result = await _purge(db, store)
    finally:
        await dispose_engine()
    logger.info("geometry retention: %s", result)
    return result
