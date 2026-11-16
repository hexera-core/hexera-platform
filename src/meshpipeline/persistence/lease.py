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

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

import meshpipeline.settings.runtime as rtcfg
from meshpipeline.persistence.job_state import TERMINAL_STATES
from meshpipeline.persistence.models import JobStatus, SimulationJob

logger = logging.getLogger(__name__)


# WHY A RENEWAL WAS REFUSED, because the four reasons are not the same kind of thing and treating
# them alike cost a finished mesh.
#
# `heartbeat` returned a bare False whether the row was gone, the generation had moved on, the
# token had been rotated, or the deadline had simply passed - and BOTH renewal paths (the beat in
# application/pipeline_run and the keeper subprocess) took any False as permanent supersession and
# stopped for good. Two of the four are permanent. A deadline is not: a later beat that sees fresh
# progress may legitimately move it forward again, so a single refusal at a quiet moment ended
# renewal for the rest of a run that was still working.
#
# It also made the logs lie. On job 4860d75f the beat printed "a newer generation/token owns this
# job" at 20:26:45.474 - 0.6 s BEFORE generation 2 was claimed. Nothing owned it; the reaper had
# just cleared the token.
RENEWED = "renewed"
#: Another generation or another token holds the job. Nothing this worker does can win it back.
SUPERSEDED = "superseded"
#: The job row is gone or has reached a terminal state. Also final.
TERMINAL = "terminal"
#: Past the whole-pipeline deadline with no progress recent enough to move it. REVERSIBLE: the
#: very next beat may carry an extension earned by progress and be renewed.
PAST_DEADLINE = "past_deadline"


def ends_renewal(reason: str) -> bool:
    """Should a caller stop renewing for good on this answer?

    Only for a refusal that can never reverse. A deadline refusal is a `hold`, not a `stop`.
    """
    return reason in (SUPERSEDED, TERMINAL)


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


def _locked_job(job_id: uuid.UUID):
    """The job row, row-locked AND as the database holds it under that lock.

    `populate_existing` is what makes the lock worth taking. Without it SQLAlchemy hands back the
    instance already in the session's identity map with the attributes of whichever read put it
    there - a read taken BEFORE the lock - so every check made on it decides on a status the lock
    never saw. claim_delivery reads the job before claiming it, so an owner's cancel committed in
    between was invisible: the claim wrote `running` over `cancelled` and the mesh ran and was
    delivered anyway (shared dev, 2026-09-29).

    It also overwrites any UNFLUSHED edit to this row in the same session. Lock first, then write -
    the only order in which the lock protects the write at all.

    FOR NO KEY UPDATE, not FOR UPDATE: it still shuts out every other writer of the row (a second
    claim, a heartbeat, a cancel), but no longer a row that only REFERENCES the job (a foreign key
    takes FOR KEY SHARE). Under FOR UPDATE a heartbeat waiting on its commit blocked the worker's own
    capture insert (capture_operations.job_id), and that insert, made synchronously on the event loop,
    kept the heartbeat from ever committing: dev-areen 2026-10-23, two FEA jobs hung an hour after a
    clean mesh, the heartbeat "idle in transaction" and the insert waiting on its transaction id.
    No lease operation changes the job's key, so nothing here needs the stronger lock.
    """
    return (select(SimulationJob).where(SimulationJob.id == job_id)
            .with_for_update(key_share=True).execution_options(populate_existing=True))


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

        row = (await db.execute(_locked_job(job_id))).scalar_one_or_none()
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
                        now: datetime | None = None, lease_seconds: int | None = None,
                        extend_deadline_to: datetime | None = None) -> bool:
        """Renew the lease. True when it was renewed, False for any of the four refusals.

        KEPT AS A BOOL on purpose: it is the contract every other caller and a dozen tests already
        assert on. A caller that must tell a reversible refusal from a permanent one - the beat and
        the keeper, which otherwise stop renewing for good on a deadline that a later beat could
        legitimately move - asks `heartbeat_reason` instead and passes the answer to `ends_renewal`.
        """
        return (await self.heartbeat_reason(
            db, own, now=now, lease_seconds=lease_seconds,
            extend_deadline_to=extend_deadline_to)) == RENEWED

    async def heartbeat_reason(self, db: AsyncSession, own: ExecutionOwnership, *,
                               now: datetime | None = None, lease_seconds: int | None = None,
                               extend_deadline_to: datetime | None = None) -> str:
        """Renew the lease and say what happened: RENEWED, SUPERSEDED, TERMINAL or PAST_DEADLINE."""
        now = now or _now()
        lease_seconds = int(lease_seconds if lease_seconds is not None else rtcfg.WORKER_LEASE_SECONDS)
        row = (await db.execute(_locked_job(own.job_id))).scalar_one_or_none()
        if row is None or row.status in TERMINAL_STATES:
            return TERMINAL
        if (row.execution_generation != own.execution_generation
                or row.active_worker_token != own.worker_token):
            return SUPERSEDED                                  # fenced: an old token/generation
        deadline = _aware(row.pipeline_deadline_at)
        extend_to = _aware(extend_deadline_to)
        if extend_to is not None and deadline is not None and extend_to > deadline:
            # PROGRESS MOVES THE DEADLINE ON (application/pipeline_budget.progress_extension): the
            # owner saw its job make progress recently, so the job is not out of time - its deadline
            # becomes a stall window after that progress. Only the verified owner can do this (the
            # generation and token were compared above), and only forward. A run with no recent
            # progress passes no extension, and the refusal below ends it exactly as before.
            deadline = extend_to
            row.pipeline_deadline_at = extend_to
        if deadline is not None and now > deadline:
            # Past the whole-pipeline deadline nothing may extend ownership - neither the
            # periodic beat nor the publish-seam recovery. A worker wedged past its deadline with
            # no progress is a zombie; letting it renew would let it hold the job forever.
            #
            # REVERSIBLE, THOUGH, which is why it is named rather than merged into the refusals
            # above: the next beat may carry an extension earned by real progress and be renewed.
            # A caller that stops renewing here stops for a condition that may already be over.
            return PAST_DEADLINE
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
        return RENEWED

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
        row = (await db.execute(_locked_job(own.job_id))).scalar_one_or_none()
        if row is None:
            return None
        if (row.execution_generation != own.execution_generation
                or row.active_worker_token != own.worker_token):
            return None
        return row

    async def evict_owner(self, db: AsyncSession, job_id: uuid.UUID, *,
                          now: datetime | None = None) -> bool:
        """Withdraw execution ownership from whoever holds it - the cancel's half of a takeover.

        The owner's cancel authority calls this in its own transaction, BEFORE the terminal CAS.
        It is `claim_execution`'s revoke-then-rotate with no successor: the mirror the current
        token authorises is revoked while the row is locked, then the token is cleared, so every
        fence check the running worker still makes fails from this commit on - `is_current_owner`
        before delivery, `lock_current_owner` at the atomic terminal transaction, and the heartbeat
        that could otherwise `heal` the mirror. Returns True when a holder was evicted, False when
        nothing held the job (a pending job no worker had claimed).

        The mirror revoke is best-effort here, unlike the takeover's, and that is safe: the row is
        the authority every terminal write is checked against, and a mirror that outlives its
        claim can never be renewed (heartbeat verifies the row first and refuses), so it can only
        expire. Failing the cancel over a Redis blip would leave the owner unable to stop a run
        for no safety gain.
        """
        row = (await db.execute(_locked_job(job_id))).scalar_one_or_none()
        if row is None or row.active_worker_token is None:
            return False
        # From here on, the row's own id: the value the database returned, not the argument.
        held_id = str(row.id)
        _ops = _fence_ops()
        try:
            _ops.revoke(held_id,
                        _ops.fingerprint(held_id, int(row.execution_generation or 0),
                                         row.active_worker_token))
        except Exception:  # noqa: BLE001 - see the docstring: the row is the authority
            logger.warning("could not revoke the execution fence for job %s on cancel - the "
                           "mirror expires on its own and the row no longer authorises it", held_id)
        row.active_worker_token = None
        row.lease_expires_at = now or _now()
        await db.flush()
        return True

    async def release(self, db: AsyncSession, own: ExecutionOwnership,
                      *, now: datetime | None = None) -> None:
        row = (await db.execute(_locked_job(own.job_id))).scalar_one_or_none()
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

    async def hand_back(self, db: AsyncSession, own: ExecutionOwnership,
                        *, now: datetime | None = None) -> bool:
        """Give a RUNNING job back to the queue, because the worker holding it is going away.

        `release`'s revoke-then-clear, plus the status: the job goes back to `pending`, the state
        a job waiting for a worker is in, with no owner. The next worker's claim then sees no live
        lease (there is no token to hold one) and takes it as a new generation - a fresh checkpoint
        thread, the same takeover a lapsed lease has always led to, just without the half hour of
        waiting for it. `started_at` and `pipeline_deadline_at` are left alone: the run's budget
        keeps counting from its first start, however many machines it passes through.

        `lease_expires_at` is set to `now` and means, on an unowned pending job, "released, but not
        yet confirmed back on the queue". The message is published only after this commits, and a
        process that dies in between would otherwise strand a pending job no message will ever
        reach. `confirm_requeued` clears it once the launcher has taken the message; until then the
        stalled-job reaper treats the job as released-but-unqueued and launches it itself.

        Only the holder can do this. The compare-and-set names this ownership's generation AND
        token, so a worker whose job was cancelled or taken over meanwhile changes nothing and is
        told so (False). The mirror is revoked first, while this worker is still the owner, so no
        write of the run being stopped can be authorised by it afterwards.
        """
        now = now or _now()
        _ops = _fence_ops()
        try:
            _ops.revoke(str(own.job_id),
                        _ops.fingerprint(str(own.job_id), own.execution_generation,
                                         own.worker_token))
        except Exception:  # noqa: BLE001 - the row below is the authority; the mirror expires
            logger.warning("could not revoke the execution fence for job %s on hand-back",
                           own.job_id)
        res = await db.execute(
            update(SimulationJob).where(
                SimulationJob.id == own.job_id,
                SimulationJob.status.in_([JobStatus.running]),
                SimulationJob.execution_generation == own.execution_generation,
                SimulationJob.active_worker_token == own.worker_token,
            ).values(status=JobStatus.pending, active_worker_token=None,
                     lease_expires_at=now, updated_at=now))
        return bool(res.rowcount == 1)

    async def confirm_requeued(self, db: AsyncSession, job_id: uuid.UUID,
                               released_at: datetime) -> bool:
        """The launcher took the message for a job released at `released_at`: it is on the queue,
        so the reaper must leave it to the next worker rather than launch it a second time.

        Only the job exactly as that release left it - still pending, unowned, carrying that
        release's mark. A worker that already claimed it (the message can be taken before this
        runs) has moved it on, and so has a cancel; neither is touched."""
        res = await db.execute(
            update(SimulationJob).where(
                SimulationJob.id == job_id,
                SimulationJob.status.in_([JobStatus.pending]),
                SimulationJob.active_worker_token.is_(None),
                SimulationJob.lease_expires_at == released_at,
            ).values(lease_expires_at=None))
        return bool(res.rowcount == 1)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


__all__ = ["ClaimResult", "ExecutionOwnership", "LeaseRepository"]
