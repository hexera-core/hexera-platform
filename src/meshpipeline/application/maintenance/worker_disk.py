# Responsibility: Free the worker's own disk of finished jobs' directories, before it fills and every job on the machine fails.
# Boundaries: runs INSIDE the worker - the one process that can see its disk; reads job status, and
#             deletes only the directories of jobs that can never run again.
from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import meshpipeline.settings.policy as polcfg
import meshpipeline.settings.runtime as rtcfg

logger = logging.getLogger(__name__)

# WHY THIS EXISTS. Every attempt of a job writes a workspace (agents/builder/workspace.py,
# WORKSPACE_BASE/<job>/generation_<g>/attempt_<n>), the geometry is materialised beside it
# (geometry_materializer.execution_workspace, WORKSPACE_BASE/<job>/geometry) and the viewer preview
# is copied into JOBS_DIR/<job>/ (post_terminal.copy_viewer_preview). On the hosted fleet these
# sit on the worker VM's boot disk (deploy/gcp/worker/startup.sh), and nothing else can delete
# them: the maintenance sweep is a Cloud Run job with no access to that disk
# (runtime/maintenance_sweep.py), and the floor VM is never retired, so its disk is never replaced.
# A 7M-cell mesh leaves about a gigabyte per attempt; a large customer job fills the disk in a few
# runs, and from then on every job on that machine fails. So the worker clears up after itself,
# each time it starts a job.
#
# WHAT IS SAFE TO DELETE. Only a directory named by a job id whose row says the job is TERMINAL
# (succeeded, failed, cancelled - persistence/job_state.py: no transition ever leaves those, so the
# job can never be resumed or redelivered here). A succeeded job's deliverables are in the object
# store before success is committed (application/artifact_uploader.py), so the local copy is only
# a cache. Never deleted, whatever the disk says:
#   - a job that is pending, running, queued or parked for review - it may resume on this host;
#   - a job this run names as kept (the run's own job, and the parent of a dispute it is about to
#     re-review - application/job_service.final_attempt_workspace reads that from local disk);
#   - the parent of ANY unfinished dispute, for a sibling worker sharing this disk;
#   - a job whose conversation export has not finished reading it (the corpus sentinel);
#   - a directory whose name is not a job id, or whose job the database does not know.
#
# THE TWO RULES.
#   1. Routine: a terminal job's directories go once it has been over for KEEP_SUCCEEDED_HOURS
#      (succeeded) or FAILED_JOB_RETENTION_HOURS (failed, cancelled - the same window the compose
#      purge uses). The window is what keeps a dispute of a recent job working when it lands on
#      the same machine.
#   2. Disk pressure: when the filesystem under either root has less than LOW_FREE_FRACTION free,
#      the window is dropped and terminal jobs are cleared oldest-ended first until
#      TARGET_FREE_FRACTION is free again. A cancelled job still needs one full lease since it
#      ended: its fenced run may still be winding down on a sibling slot.

#: How long a succeeded job's local workspace is kept for a dispute that lands on this machine.
KEEP_SUCCEEDED_HOURS = 24

#: Below this fraction of the disk free, the keep window is dropped (15 GB on a 100 GB worker disk).
LOW_FREE_FRACTION = 0.15

#: ...and old job directories are cleared until this fraction is free again.
TARGET_FREE_FRACTION = 0.30

#: The files post_terminal.copy_viewer_preview writes into JOBS_DIR/<job>/. Only these are deleted
#: there - the directory itself goes only if nothing else is left in it.
PREVIEW_GLOB = "preview.*"

_IDS_PER_QUERY = 500


@dataclass(frozen=True)
class JobFacts:
    status: object                 # JobStatus
    ended_at: datetime | None


def _job_id_dirs(root: Path) -> dict[str, Path]:
    """Direct children of `root` that are real directories named by a canonical job id."""
    found: dict[str, Path] = {}
    try:
        children = list(root.iterdir())
    except OSError:
        return found
    for child in children:
        try:
            if child.is_symlink() or not child.is_dir():
                continue
            if str(uuid.UUID(child.name)) != child.name:
                continue
        except (ValueError, OSError):
            continue
        found[child.name] = child
    return found


def _free_fraction(path: Path) -> float:
    usage = shutil.disk_usage(path)
    return usage.free / usage.total if usage.total else 1.0


def pending_export_job_ids() -> set[str]:
    """Jobs whose conversation export has started and not finished: it reads their workspace."""
    pending: set[str] = set()
    corpus = Path(rtcfg.CORPUS_DIR)
    if not corpus.is_dir():
        return pending
    for sample in corpus.iterdir():
        try:
            marker = sample / ".job_id"
            if sample.is_dir() and marker.exists() and not (sample / ".export_complete").exists():
                pending.add(marker.read_text().strip())
        except OSError:
            continue
    return pending


def _aware(moment: datetime | None) -> datetime | None:
    if moment is not None and moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment


def plan_reclaim(facts: dict[str, JobFacts], *, now: datetime, keep: set[str],
                 under_pressure: bool) -> list[str]:
    """Which jobs' directories may go, oldest-ended first. Pure: the policy, without the disk."""
    from meshpipeline.persistence.job_state import TERMINAL_STATES
    from meshpipeline.persistence.models import JobStatus

    lease = timedelta(seconds=int(rtcfg.WORKER_LEASE_SECONDS))
    chosen: list[tuple[datetime, str]] = []
    for job_id, f in facts.items():
        if job_id in keep or f.status not in TERMINAL_STATES:
            continue
        ended = _aware(f.ended_at)
        if under_pressure:
            if f.status == JobStatus.cancelled and (ended is None or ended > now - lease):
                continue
        else:
            hours = (KEEP_SUCCEEDED_HOURS if f.status == JobStatus.succeeded
                     else polcfg.FAILED_JOB_RETENTION_HOURS)
            if ended is None or ended > now - timedelta(hours=hours):
                continue
        chosen.append((ended or datetime.min.replace(tzinfo=UTC), job_id))
    return [job_id for _, job_id in sorted(chosen)]


def _remove(job_id: str, workspace: Path | None, record: Path | None) -> bool:
    """Delete one job's directories. True when its workspace is gone."""
    workspace_gone = False
    if workspace is not None:
        try:
            shutil.rmtree(workspace)
            workspace_gone = True
        except FileNotFoundError:
            workspace_gone = True
        except OSError as exc:
            logger.warning("disk reclaim: could not remove %s: %s", workspace, exc)
    if record is not None:
        try:
            for preview in record.glob(PREVIEW_GLOB):
                if preview.is_file() and not preview.is_symlink():
                    preview.unlink()
            record.rmdir()               # only when nothing else lives there
        except OSError:
            pass
    return workspace_gone


async def _load_facts(session_factory, job_ids: Iterable[str]) -> dict[str, JobFacts]:
    from sqlalchemy import select

    from meshpipeline.persistence.models import SimulationJob

    ids = [uuid.UUID(j) for j in job_ids]
    facts: dict[str, JobFacts] = {}
    async with session_factory() as db:
        for i in range(0, len(ids), _IDS_PER_QUERY):
            rows = await db.execute(
                select(SimulationJob.id, SimulationJob.status, SimulationJob.ended_at)
                .where(SimulationJob.id.in_(ids[i:i + _IDS_PER_QUERY])))
            for job_id, status, ended_at in rows.all():
                facts[str(job_id)] = JobFacts(status=status, ended_at=ended_at)
    return facts


async def _unfinished_dispute_parents(session_factory) -> set[str]:
    """Parents of disputes not yet finished: their workspace is what the dispute re-reviews."""
    from sqlalchemy import select

    from meshpipeline.persistence.job_state import TERMINAL_STATES
    from meshpipeline.persistence.models import SimulationJob

    parents: set[str] = set()
    async with session_factory() as db:
        rows = await db.execute(
            select(SimulationJob.dispatch_payload)
            .where(SimulationJob.status.notin_(list(TERMINAL_STATES))))
        for (payload,) in rows.all():
            dispute = (payload or {}).get("user_dispute") if isinstance(payload, dict) else None
            parent = str((dispute or {}).get("of_job_id", "") or "").strip()
            if parent:
                parents.add(parent)
    return parents


async def _mark_purged(session_factory, job_ids: list[str]) -> None:
    """Record that these jobs' workspaces are gone, so a dispute of one is refused truthfully
    (api/v1/simulation.py) instead of failing on a missing directory. Not a status write; the
    status guard only re-asserts that the job is still terminal."""
    from sqlalchemy import update

    from meshpipeline.persistence.job_state import TERMINAL_STATES
    from meshpipeline.persistence.models import SimulationJob

    async with session_factory() as db:
        await db.execute(
            update(SimulationJob)
            .where(SimulationJob.id.in_([uuid.UUID(j) for j in job_ids]),
                   SimulationJob.status.in_(list(TERMINAL_STATES)))
            .values(workspace_purged=True))
        await db.commit()


async def reclaim_finished_job_dirs(session_factory, *, keep: Iterable[str] = (),
                                    now: datetime | None = None,
                                    free_fraction: Callable[[Path], float] = _free_fraction) -> dict:
    """Delete finished jobs' directories under WORKSPACE_BASE and JOBS_DIR (see the rules above).

    Fail-closed: a job whose state cannot be read is kept, and a database error deletes nothing."""
    now = now or datetime.now(UTC)
    ws_root, jobs_root = Path(rtcfg.WORKSPACE_BASE), Path(rtcfg.JOBS_DIR)
    workspaces = await asyncio.to_thread(_job_id_dirs, ws_root)
    records = await asyncio.to_thread(_job_id_dirs, jobs_root)
    candidates = set(workspaces) | set(records)
    if not candidates:
        return {"removed": 0, "under_pressure": False}

    held = {str(k) for k in keep if k}
    held |= await _unfinished_dispute_parents(session_factory)
    held |= await asyncio.to_thread(pending_export_job_ids)
    facts = await _load_facts(session_factory, candidates)

    roots = [r for r in (ws_root, jobs_root) if r.is_dir()]

    def _low(limit: float) -> bool:
        return any(free_fraction(r) < limit for r in roots)

    under_pressure = _low(LOW_FREE_FRACTION)
    routine = plan_reclaim(facts, now=now, keep=held, under_pressure=False)
    extra = ([j for j in plan_reclaim(facts, now=now, keep=held, under_pressure=True)
              if j not in set(routine)] if under_pressure else [])

    removed: list[str] = []
    purged: list[str] = []

    async def _drop(job_id: str) -> None:
        gone = await asyncio.to_thread(_remove, job_id, workspaces.get(job_id), records.get(job_id))
        removed.append(job_id)
        if gone and job_id in workspaces:
            purged.append(job_id)

    for job_id in routine:
        await _drop(job_id)
    for job_id in extra:
        if not _low(TARGET_FREE_FRACTION):
            break
        await _drop(job_id)

    if purged:
        await _mark_purged(session_factory, purged)
    if under_pressure and _low(LOW_FREE_FRACTION):
        logger.warning("disk reclaim: still under %.0f%% free after clearing %d finished job(s) - "
                       "the rest belongs to jobs that may still run", LOW_FREE_FRACTION * 100,
                       len(removed))
    if removed:
        logger.info("disk reclaim: removed %d finished job director%s (disk pressure: %s)",
                    len(removed), "y" if len(removed) == 1 else "ies", under_pressure)
    return {"removed": len(removed), "under_pressure": under_pressure}


async def reclaim_quietly(session_factory, *, keep: Iterable[str] = (), jlog=None) -> dict:
    """The run's call: never raises. Freeing disk must never cost the job that asked for it."""
    try:
        return await reclaim_finished_job_dirs(session_factory, keep=keep)
    except Exception as exc:                       # noqa: BLE001 - housekeeping never fails a run
        (jlog or logger).warning("disk reclaim skipped: %s", exc)
        return {"removed": 0, "under_pressure": False, "error": str(exc)}
