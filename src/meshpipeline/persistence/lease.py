# Responsibility: Own durable, exclusive execution ownership of a job.
# Owns: the claim, the heartbeat, the execution generation and the top-level deadline anchored at the first claim.
# Boundaries: ownership is a database fact, not a process fact.
# Collaborates with: application/execution_fence.py.
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import meshpipeline.settings.runtime as rtcfg
from meshpipeline.persistence.job_state import TERMINAL_STATES
from meshpipeline.persistence.models import JobStatus, SimulationJob

logger = logging.getLogger(__name__)


#: One seam, so a test can observe the ORDER of the mirror calls relative to the PostgreSQL
#: transaction - which is the whole safety property.
def _fence_ops():
    from meshpipeline.adapters.event_stream import fence
    return fence


class FenceUnavailable(RuntimeError):
    pass


#: The adapter call itself lives here, beside revoke and refresh, because `application` may not
#: import a concrete adapter. The application keeps its own module-level seam over this one.
def install_fence_value(job_id: str, fingerprint: str, ttl_seconds: int) -> bool:
    return bool(_fence_ops().install(job_id, fingerprint, ttl_seconds))


class ClaimResult(str, Enum):
    acquired_new_generation = "acquired_new_generation"   # initial claim OR different-execution takeover
    resumed_same_generation = "resumed_same_generation"   # same-execution restart / re-entrant token
    active_lease_conflict = "active_lease_conflict"       # a live different owner exists - DO NOT proceed
    already_terminal = "already_terminal"                 # job is succeeded/failed - nothing to run
    invalid_job_state = "invalid_job_state"               # not in a runnable (pending/running) state
    not_found = "not_found"


@dataclass(frozen=True)
class ExecutionOwnership:
    job_id: uuid.UUID
    execution_generation: int
    worker_token: uuid.UUID
    backend: str
    pipeline_deadline_at: datetime | None
    claim_epoch: int = 0
    #: remaining lease at the moment the claim was written, so the Redis mirror
    #: installed from it can only ever expire EARLIER than this claim
    fence_ttl_seconds: int = 0

    def token_hash(self) -> str:
        import hashlib
        return hashlib.sha256(str(self.worker_token).encode()).hexdigest()[:12]


def _now() -> datetime:
    return datetime.now(UTC)


class LeaseRepository:
    async def claim_execution(
        self, db: AsyncSession, job_id: uuid.UUID, *, worker_token: uuid.UUID, backend: str,
        backend_execution_id: str | None = None, now: datetime | None = None,
        lease_seconds: int | None = None, pipeline_total_seconds: int | None = None,
    ) -> tuple[ClaimResult, ExecutionOwnership | None]:
        now = now or _now()
        lease_seconds = int(lease_seconds if lease_seconds is not None else rtcfg.WORKER_LEASE_SECONDS)
        pipeline_total = int(pipeline_total_seconds if pipeline_total_seconds is not None
                             else rtcfg.PIPELINE_TOTAL_TIMEOUT_SECONDS)

        row = (await db.execute(
            select(SimulationJob).where(SimulationJob.id == job_id).with_for_update())).scalar_one_or_none()
        if row is None:
            return ClaimResult.not_found, None
        if row.status in TERMINAL_STATES:
            return ClaimResult.already_terminal, None
        if row.status not in (JobStatus.pending, JobStatus.running):
            return ClaimResult.invalid_job_state, None

        _expires = _aware(row.lease_expires_at)
        lease_active = (row.active_worker_token is not None
                        and _expires is not None
                        and _expires > now)
        if lease_active and row.active_worker_token != worker_token:
            return ClaimResult.active_lease_conflict, None

        same_execution = bool(
            backend_execution_id and row.pipeline_execution_id
            and backend_execution_id == row.pipeline_execution_id and (row.execution_generation or 0) > 0)
        reentrant = lease_active and row.active_worker_token == worker_token

        if reentrant or same_execution:
            new_gen = row.execution_generation or 1
            result = ClaimResult.resumed_same_generation
        else:
            new_gen = (row.execution_generation or 0) + 1     # initial (0→1) OR different-exec takeover
            result = ClaimResult.acquired_new_generation

        # REVOKE THE OUTGOING FENCE while the row is still locked, so no new owner can be
        # exposed while a superseded worker's mirror could still authorize a write. A resumed
        # same-generation claim keeps its own fence and revokes nothing.
        _ops = _fence_ops()
        if result is ClaimResult.acquired_new_generation and row.active_worker_token is not None:
            try:
                _ops.revoke(str(job_id),
                            _ops.fingerprint(str(job_id), int(row.execution_generation or 0),
                                             row.active_worker_token))
            except Exception as exc:  # noqa: BLE001
                raise FenceUnavailable(
                    f"could not revoke the outgoing execution fence for job {job_id}") from exc

        row.status = JobStatus.running
        row.execution_generation = new_gen
        # ONE increment per successful claim, in the same row-locked transaction as the token
        # rotation - a continuation keeps its generation but must still order after its
        # predecessor, and a timestamp cannot do that safely across workers.
        row.execution_claim_epoch = int(row.execution_claim_epoch or 0) + 1
        row.active_worker_token = worker_token                # install / rotate - old token now fenced
        row.owning_backend = backend
        if backend_execution_id:
            row.pipeline_execution_id = backend_execution_id
        row.lease_acquired_at = now
        row.lease_heartbeat_at = now
        row.lease_expires_at = now + timedelta(seconds=lease_seconds)
        if row.started_at is None:
            row.started_at = now
        # anchor the durable top-level deadline ONCE, at the FIRST execution start (not
        # created_at - a delayed approval / deferred launch must not consume execution budget).
        if row.pipeline_deadline_at is None:
            row.pipeline_deadline_at = now + timedelta(seconds=pipeline_total)
        await db.flush()
        return result, ExecutionOwnership(
            job_id=job_id, execution_generation=new_gen, worker_token=worker_token,
            backend=backend, pipeline_deadline_at=_aware(row.pipeline_deadline_at),
            claim_epoch=int(row.execution_claim_epoch),
            fence_ttl_seconds=self.fence_ttl_seconds(row, now))

    async def heartbeat(self, db: AsyncSession, own: ExecutionOwnership, *,
                        now: datetime | None = None, lease_seconds: int | None = None) -> bool:
        now = now or _now()
        lease_seconds = int(lease_seconds if lease_seconds is not None else rtcfg.WORKER_LEASE_SECONDS)
        row = (await db.execute(
            select(SimulationJob).where(SimulationJob.id == own.job_id).with_for_update())).scalar_one_or_none()
        if row is None or row.status in TERMINAL_STATES:
            return False
        if (row.execution_generation != own.execution_generation
                or row.active_worker_token != own.worker_token):
            return False                                       # fenced: an old token/generation
        row.lease_heartbeat_at = now
        row.lease_expires_at = now + timedelta(seconds=lease_seconds)
        await db.flush()
        # ONLY THEN the mirror, and only if it is still ours. `refresh` never SETs, so a delayed
        # heartbeat from a superseded worker cannot bring a revoked fence back.
        # A refresh that reports 0 found no fence of ours to extend, and discarding that answer is
        # how a healthy job dies: the mirror can never return, every later beat no-ops in silence,
        # PostgreSQL goes on reporting perfect health, and the next fenced publish is refused for a
        # supersession that never happened.
        #
        # `heal` is NX, so it can only fill a hole. It cannot displace a newer generation's fence -
        # that write simply loses - which is the takeover `refresh`'s never-SET rule was protecting
        # against. And the row above was read FOR UPDATE with its generation and token compared to
        # ours, so PostgreSQL has already confirmed on this line that the fence being restored is
        # the one that belongs here. A superseded worker returned False long before reaching it.
        _ops = _fence_ops()
        _fp = _ops.fingerprint(str(own.job_id), own.execution_generation, own.worker_token)
        _ttl = self.fence_ttl_seconds(row, now)
        try:
            if not _ops.refresh(str(own.job_id), _fp, _ttl) and _ops.heal(str(own.job_id), _fp, _ttl):
                logger.warning("execution fence for job %s had lapsed - restored from the claim "
                               "this heartbeat verified", own.job_id)
        except Exception:  # noqa: BLE001 - a mirror blip costs availability, never safety
            logger.warning("could not refresh the execution fence for job %s", own.job_id)
        return True

    # Remaining PostgreSQL lease, measured AFTER the write, so query and network time shorten
    # the mirror rather than letting it outlive the claim it stands for. Pure row-derived
    # logic - the INSTALL itself is the application's, not this substitutable repository's.
    @staticmethod
    def fence_ttl_seconds(row, now: datetime | None = None) -> int:
        expires = _aware(row.lease_expires_at)
        if expires is None:
            return 1
        return max(1, int((expires - (now or _now())).total_seconds()))

    async def is_current_owner(self, db: AsyncSession, own: ExecutionOwnership) -> bool:
        row = (await db.execute(
            select(SimulationJob).where(SimulationJob.id == own.job_id))).scalar_one_or_none()
        if row is None or row.status in TERMINAL_STATES:
            return False
        return (row.execution_generation == own.execution_generation
                and row.active_worker_token == own.worker_token)

    async def lock_current_owner(self, db: AsyncSession,
                                 own: ExecutionOwnership) -> SimulationJob | None:
        row = (await db.execute(select(SimulationJob).where(SimulationJob.id == own.job_id)
                                .with_for_update())).scalar_one_or_none()
        if row is None:
            return None
        if (row.execution_generation != own.execution_generation
                or row.active_worker_token != own.worker_token):
            return None
        return row

    async def release(self, db: AsyncSession, own: ExecutionOwnership,
                      *, now: datetime | None = None) -> None:
        row = (await db.execute(
            select(SimulationJob).where(SimulationJob.id == own.job_id).with_for_update())).scalar_one_or_none()
        if row is None or row.status in TERMINAL_STATES:
            return
        if (row.execution_generation == own.execution_generation
                and row.active_worker_token == own.worker_token):
            # Before ownership is given up, so a worker that already passed its PostgreSQL check
            # cannot still be authorized by the mirror afterwards. A mismatched fence belongs to
            # someone else and is left alone.
            _ops = _fence_ops()
            try:
                _ops.revoke(str(own.job_id),
                            _ops.fingerprint(str(own.job_id), own.execution_generation,
                                             own.worker_token))
            except Exception:  # noqa: BLE001
                logger.warning("could not revoke the execution fence for job %s", own.job_id)
            row.active_worker_token = None
            row.lease_expires_at = now or _now()
            await db.flush()


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


__all__ = ["ClaimResult", "ExecutionOwnership", "LeaseRepository"]
