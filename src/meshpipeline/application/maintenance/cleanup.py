# Responsibility: Reclaim what finished runs leave behind: workspaces, event logs and stalled jobs.
# Boundaries: bounded, resumable sweeps; each is safe to interrupt and safe to run twice.
from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import meshpipeline.settings.policy as polcfg
import meshpipeline.settings.providers as provcfg
import meshpipeline.settings.runtime as rtcfg

if TYPE_CHECKING:
    from sqlalchemy.sql.elements import ColumnElement

logger = logging.getLogger(__name__)


def _publish_terminal_log(job_id: str, closing_text: str = "") -> None:
    try:
        from meshpipeline.contracts.event_stream import publisher
        from meshpipeline.persistence.repositories.terminal_outbox_repository import (
            dedup_key_for,
        )
        if not closing_text:
            from meshpipeline.errors import FailureClass, user_message_for
            closing_text = user_message_for(FailureClass.WORKER_LOST)
        # same terminal identity: a cleanup retry must not add a second ending
        publisher(job_id).closing(closing_text, dedup_key_for(job_id))
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
                # a cancelled run's workspace is purged like a failed one's: nothing in it was
                # delivered, and nothing ever will be
                .where(SimulationJob.status.in_([JobStatus.failed, JobStatus.cancelled]))
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


def reap_stalled_jobs() -> dict:
    import asyncio
    return asyncio.run(_reap_stalled_async())


def stalled_jobs_clause(now: datetime) -> ColumnElement[bool]:
    """The WHERE that selects a job nothing will finish. `now` is passed in, so the cutoffs it
    derives can be read back.

    TWO RULES, and either is enough.

    THE LEASE. A running job's owner heartbeats every WORKER_HEARTBEAT_SECONDS, and each beat
    extends `lease_expires_at` by WORKER_LEASE_SECONDS (persistence/lease.py). A lease that has
    been expired for a WHOLE further lease period is a worker that is gone, not late - the fleet's
    autoscaler replaced its instance, or the container was killed. Nothing takes such a job over:
    a task is acknowledged on receipt (celery_app.py), so it is never redelivered. Until this rule
    existed it sat as `running` for STALLED_JOB_TIMEOUT_HOURS, holding back its organisation's
    base credits, so the tenant was refused every new job for four hours (application/spend_gate.py;
    shared dev, 2026-09-28). The grace is one full lease rather than a setting of its own: the
    execution fence already treats a lapse of one lease as a lost mirror to heal, so two is the
    first duration with no legitimate explanation, and a heartbeat thirty minutes late on a
    sixty-second cadence is not a hiccup. Past the pipeline deadline no heartbeat may extend the
    lease at all, so this is also what fails a zombie run within the hour of its ceiling.

    THE CEILING. A job that was created and never picked up, or that reached `running` WITHOUT
    EVER HOLDING A LEASE and started, more than STALLED_JOB_TIMEOUT_HOURS ago. A running job with
    a lease is judged by the lease alone: with a live one it is alive whatever its age, and the
    ceiling here (four hours) is SHORTER than the pipeline deadline (six), so an age rule applied
    to leased jobs would have failed every live job between its fourth and sixth hour the first
    time the reaper ran on a hosted deployment - and the worker's real result would then have been
    refused by its own terminal compare-and-set. A run that goes on too long is ended by the
    pipeline deadline, not by this: past it no heartbeat may extend the lease, and the lease rule
    follows within the hour.
    """
    from sqlalchemy import or_

    from meshpipeline.persistence.models import JobStatus, SimulationJob

    started_cutoff = now - timedelta(hours=polcfg.STALLED_JOB_TIMEOUT_HOURS)
    lease_cutoff = now - timedelta(seconds=int(rtcfg.WORKER_LEASE_SECONDS))
    return or_(
        # the owner stopped heartbeating (instance replaced, container killed)
        (SimulationJob.status == JobStatus.running)
        & SimulationJob.lease_expires_at.isnot(None)
        & (SimulationJob.lease_expires_at < lease_cutoff),
        # reached `running` without a claim (a row from before leases), and long ago
        (SimulationJob.status == JobStatus.running)
        & SimulationJob.lease_expires_at.is_(None)
        & SimulationJob.started_at.isnot(None)
        & (SimulationJob.started_at < started_cutoff),
        # never picked up (started_at is NULL for these)
        SimulationJob.status.in_([JobStatus.pending, JobStatus.queued])
        & (SimulationJob.created_at < started_cutoff),
    )


async def _reap_stalled_async() -> dict:
    from sqlalchemy import select, update
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from meshpipeline.application.final_result import (
        never_started_result,
        render_message,
        worker_lost_result,
    )
    from meshpipeline.persistence.models import FailedReason, JobStatus, SimulationJob

    _engine = create_async_engine(provcfg.POSTGRES_DSN, echo=False, pool_size=2,
                                  max_overflow=2, pool_pre_ping=True)
    _SessionLocal = async_sessionmaker(bind=_engine, class_=AsyncSession,
                                       expire_on_commit=False, autocommit=False, autoflush=False)
    now = datetime.now(UTC)
    reaped: list[str] = []
    try:
        async with _SessionLocal() as db:
            rows = (await db.execute(
                select(SimulationJob).where(stalled_jobs_clause(now))
            )).scalars().all()
            for job in rows:
                _was = job.status
                # THE DURABLE ACCOUNT, by the rule that selected the row. The reaper used to stamp
                # the status and nothing else, so once the event log expired the only thing left
                # to show a returning user was "Job already failed." - a job read and a
                # reconnecting socket both render this record instead (application/final_result).
                # A RUNNING row (lease lapsed, or started long ago with none) had a worker that
                # is gone; a PENDING/QUEUED row never had one, and is told so.
                _never_started = _was in (JobStatus.pending, JobStatus.queued)
                _record = (never_started_result if _never_started else worker_lost_result)(
                    job_id=str(job.id), owner_id=str(getattr(job, "owner_id", "") or ""),
                    attempts=int(getattr(job, "current_attempt", 0) or 0))
                # COMPARE-AND-SET on the state the row was SELECTED in: a job that raced to a
                # terminal result between the SELECT above and here is never overwritten by the
                # reaper, and neither is a pending job a worker claimed in that window - it is
                # running now, and "no worker picked this run up" would be false of it. The
                # failure reason is stamped only when the reaper actually failed it.
                # (Spelled `in_([...])` over the one observed state: the architecture fitness
                # suite recognises a status CAS by its `status.in_` predicate.)
                res = await db.execute(
                    update(SimulationJob).where(
                        SimulationJob.id == job.id,
                        SimulationJob.status.in_([_was]),
                    ).values(
                        status=JobStatus.failed,
                        failed_reason=FailedReason.unhandled,
                        final_result=_record.to_dict(),
                        ended_at=now,
                        updated_at=now,
                    )
                )
                if res.rowcount == 0:
                    logger.info("reap_stalled_jobs: job %s moved on from %s before reaping - "
                                "left as-is", job.id, _was)
                    continue
                reaped.append(str(job.id))
                logger.warning(
                    "reap_stalled_jobs: job %s stalled in %s (started=%s created=%s "
                    "lease_expires=%s) - marked failed(unhandled)",
                    job.id, _was, job.started_at, job.created_at,
                    getattr(job, "lease_expires_at", None),
                )
                # Publish a TERMINAL log line so any live WebSocket client
                # streaming this (crash-dropped) job receives a closing message and
                # disconnects, instead of hanging forever waiting for output the
                # dead worker will never produce. The SAME sentence the record renders.
                _publish_terminal_log(str(job.id), render_message(_record))
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
