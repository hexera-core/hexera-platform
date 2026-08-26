# Responsibility: Answer whether this worker still owns the run, and stop it if not.
# Owns: the ownership tuple, the pre-side-effect assertion, remaining time, and the delivery claim.
# Boundaries: it fences.
# Collaborates with: contracts/execution_guard.py and persistence/lease.py.
from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import NamedTuple

from meshpipeline.contracts import execution_guard as _guard
from meshpipeline.contracts.event_stream import StaleExecutionPublish
from meshpipeline.persistence.lease import ExecutionOwnership, LeaseRepository

logger = logging.getLogger(__name__)

_OWNERSHIP: ContextVar[ExecutionOwnership | None] = ContextVar("execution_ownership", default=None)
# The session factory the RUN owns. Binding it with the ownership keeps every fence check on the
# worker's own engine/pool (the one `_run_async` created) instead of a separate process-global engine
# - one pool, one configuration, and no cross-event-loop reuse.
_SESSIONS: ContextVar[object | None] = ContextVar("execution_sessions", default=None)


# The exception engines catch is the CONTRACT's (contracts/execution_guard) - an engine must be
# able to abandon superseded work without importing this orchestration module. Re-exported here so
# the application's own callers keep one import.
StaleWorkerFenced = _guard.StaleWorkerFenced


def current_ownership() -> ExecutionOwnership | None:
    return _OWNERSHIP.get()


@contextmanager
def execution_ownership(own: ExecutionOwnership | None, *, session_factory=None):
    token = _OWNERSHIP.set(own)
    stoken = _SESSIONS.set(session_factory)
    # Install the ownership check engines reach through contracts/execution_guard. They must be
    # able to stop when superseded without importing the lease, the repository or this module.
    with _guard.owner_checker(None if own is None else
                              lambda: _check(own, session_factory=session_factory)):
        try:
            yield own
        finally:
            _OWNERSHIP.reset(token)
            _SESSIONS.reset(stoken)


async def is_current_owner(*, session_factory=None) -> bool:
    own = _OWNERSHIP.get()
    if own is None:
        return True
    return await _check(own, session_factory=session_factory)


async def assert_current_owner(where: str, *, session_factory=None) -> None:
    own = _OWNERSHIP.get()
    if own is None:
        return
    if not await _check(own, session_factory=session_factory):
        logger.error("FENCE REJECT at %s - job_id=%s generation=%d token=%s is no longer the owner",
                     where, own.job_id, own.execution_generation, own.token_hash())
        raise StaleWorkerFenced(where, str(own.job_id), own.execution_generation,
                                own.token_hash())


async def reverify_and_heal_fence(own: ExecutionOwnership, *, what: str = "publish",
                                  session_factory=None) -> None:
    """One out-of-band claim re-verification after the Redis fence gate refused (-2).

    The mirror can lapse while the PostgreSQL claim is intact (heartbeats starved during a
    long synchronous stretch - the failure mode that killed a finished tugboat mesh mid-
    review). LeaseRepository.heartbeat is the ONE safe primitive for this: it re-verifies
    generation and token under the row lock, refuses past the pipeline deadline, renews the
    lease, and restores the fence via refresh-then-NX-heal - which cannot resurrect a fence a
    takeover revoked, because the takeover's row lock already rotated generation or token and
    the verify fails first. If the claim is truly gone, the original refusal stands.
    """
    if session_factory is None:
        session_factory = _SESSIONS.get()
    if session_factory is None:
        from meshpipeline.persistence.session import get_db as session_factory  # noqa: N813
    repo = LeaseRepository()
    try:
        async with session_factory() as db:
            ok = await repo.heartbeat(db, own)
            if ok:
                await db.commit()
    except Exception as exc:  # noqa: BLE001 - ambiguous ownership fails closed
        logger.error("fence recovery for job_id=%s could not re-verify the claim (%s) - "
                     "failing CLOSED", own.job_id, type(exc).__name__)
        raise StaleExecutionPublish(
            f"{what}: fence recovery could not re-verify the claim for job {own.job_id} - "
            "refusing to publish") from exc
    if not ok:
        raise StaleExecutionPublish(
            f"{what}: the claim for job {own.job_id} (generation {own.execution_generation}) "
            "no longer holds after a fence lapse - refusing to publish")
    logger.warning("execution fence for job_id=%s generation=%d healed at the publish seam - "
                   "the mirror had lapsed while the PostgreSQL claim was intact; lease renewed",
                   own.job_id, own.execution_generation)


async def remaining_pipeline_time(*, now: float | None = None) -> float:
    own = _OWNERSHIP.get()
    if own is None or own.pipeline_deadline_at is None:
        return float("inf")
    import time as _time
    return own.pipeline_deadline_at.timestamp() - (_time.time() if now is None else now)


async def _check(own: ExecutionOwnership, *, session_factory=None, _repo=LeaseRepository()) -> bool:
    if session_factory is None:
        session_factory = _SESSIONS.get()
    if session_factory is None:
        from meshpipeline.persistence.session import get_db as session_factory  # noqa: N813
    last: Exception | None = None
    for attempt in (0, 1):
        try:
            async with session_factory() as db:
                return await _repo.is_current_owner(db, own)
        except Exception as exc:  # noqa: BLE001 - classified below, never swallowed silently
            last = exc
            if attempt == 0:
                await asyncio.sleep(0.2)
    logger.error("FENCE UNRESOLVED for job_id=%s generation=%d - the ownership check could not be "
                 "completed (%s); failing CLOSED rather than acting on ambiguous ownership",
                 own.job_id, own.execution_generation, type(last).__name__)
    return False


__all__ = ["StaleWorkerFenced", "assert_current_owner", "is_current_owner", "current_ownership",
           "execution_ownership", "remaining_pipeline_time", "reverify_and_heal_fence"]


# #
# DELIVERY ADMISSION - may this delivery run at all, and under what fence?
# Extracted from application/pipeline_run._run_async, where the retry-storm guard, the atomic
# claim and the five claim-result branches sat as ~90 inline lines. They are one change reason -
# deciding whether this delivery is the active worker - and they belong beside the ownership the
# claim produces, not in the orchestrator that consumes it.
# The refusal is DATA, not an early return: the orchestrator still owns the terminal side effects
# (publishing the closing message, disposing its engine) because it owns those objects.
# #

# THE installation seam. A module-level function, not a repository method: a suite may supply
# any LeaseRepository double it likes, and none of them should have to implement Redis
# orchestration to claim delivery. Resolved lazily in the same layering-safe pattern the claim
# authority already uses for revoke and refresh.
def install_execution_fence(job_id: str, fingerprint: str, ttl_seconds: int) -> bool:
    from meshpipeline.persistence.lease import install_fence_value
    return bool(install_fence_value(job_id, fingerprint, ttl_seconds))


class DeliveryRefused(NamedTuple):

    status: str
    detail: dict


class DeliveryClaim(NamedTuple):

    ownership: ExecutionOwnership
    worker_token: uuid.UUID
    #: The claimed row's creation time - the origin of the pipeline budget deadline. Returned here
    #: because the claim is the only place the row is read, and the orchestrator needs it after.
    job_created_at: object | None


async def guard_redelivery(session_factory, job_repo, job_id: str, *, jlog, max_redeliveries: int,
                           deliveries: int):
    if deliveries <= max_redeliveries:
        return None

    from meshpipeline.errors import FailureClass, failed_reason_for, record_dead_letter
    from meshpipeline.persistence.job_state import TransitionResult
    from meshpipeline.persistence.models import FailedReason, JobStatus

    jlog.error("Job redelivered %d times (cap %d) - it keeps crashing; dead-lettering and failing it",
               deliveries, max_redeliveries)
    try:
        async with session_factory() as db:
            # CAS to failed - never overwrites an already-terminal result; only set the failure
            # reason when THIS caller actually performed the transition.
            if await job_repo.transition(db, uuid.UUID(job_id), JobStatus.failed) == TransitionResult.applied:
                row = await job_repo.get_internal(db, uuid.UUID(job_id))
                if row:
                    try:
                        row.failed_reason = FailedReason(failed_reason_for(FailureClass.RESOURCE))
                    except ValueError:
                        row.failed_reason = FailedReason.unhandled
            await db.commit()
    except Exception as exc:                       # noqa: BLE001 - the dead-letter must still record
        jlog.warning("redelivery-cap: could not mark job failed: %s", exc)
    record_dead_letter(job_id, FailureClass.RESOURCE, "worker",
                       f"redelivered {deliveries}x (cap {max_redeliveries}) - poison job")
    return DeliveryRefused("failed", {"job_id": job_id, "status": "failed",
                                      "reason": "redelivery_cap"})


async def claim_delivery(session_factory, job_repo, job_id: str, *, jlog, backend: str,
                         backend_execution_id: str):
    # Imported inside the call, not at module scope: the repository is substituted per-test on the
    # persistence module, and a module-level binding would capture the real class at import time.
    from meshpipeline.persistence.lease import ClaimResult, LeaseRepository
    from meshpipeline.persistence.models import JobStatus

    lease_repo = LeaseRepository()
    worker_token = uuid.uuid4()
    ownership = None
    async with session_factory() as db:
        existing = await job_repo.get_internal(db, uuid.UUID(job_id))
        if existing is not None and existing.status in (JobStatus.succeeded, JobStatus.failed):
            jlog.warning("Ignoring re-delivered task - job already terminal (status=%s); not re-running",
                         existing.status.value)
            return DeliveryRefused(existing.status.value,
                                   {"job_id": job_id, "status": existing.status.value,
                                    "skipped": "already_terminal"})
        claim, ownership = await lease_repo.claim_execution(
            db, uuid.UUID(job_id), worker_token=worker_token, backend=backend,
            backend_execution_id=backend_execution_id)
        await db.commit()
        # Bind the tenant every durable capture write files under - taken from the JOB ROW, never
        # from the dispatch payload. The payload is attacker-influenceable in a way the row is not,
        # and capture content is private: filing one tenant's payload under another's id is a
        # disclosure, not a bookkeeping error. A job with no row has no tenant to name, and the
        # claim below refuses it anyway - capture stays unbound rather than guessing.
        if existing is not None:
            from meshpipeline.capture import scope as _capture_scope
            _capture_scope.bind(str(existing.owner_id), job_id)

    if claim in (ClaimResult.not_found, ClaimResult.invalid_job_state):
        jlog.warning("Could not start job %s - claim=%s; not re-running", job_id, claim.value)
        return DeliveryRefused("skipped", {"job_id": job_id, "status": "skipped",
                                           "reason": "not_startable"})
    if claim == ClaimResult.already_terminal:
        return DeliveryRefused("skipped", {"job_id": job_id, "status": "skipped",
                                           "skipped": "already_terminal"})
    if claim == ClaimResult.active_lease_conflict:
        # Another worker holds an ACTIVE lease - refuse to double-run. NOT a failure: the
        # legitimate owner finalizes it; a dead owner's lease expires and a later delivery (or the
        # stalled-job reaper) takes over. Never fabricate a terminal result here.
        jlog.warning("Another worker holds an active lease on job %s - not double-running "
                     "(anti-duplicate-execution guard); the current owner finalizes it", job_id)
        return DeliveryRefused("skipped", {"job_id": job_id, "status": "skipped",
                                           "reason": "active_lease_conflict"})
    if ownership is None:
        # Unreachable by contract (a granted claim always yields ownership); fail closed rather
        # than run unfenced, since every downstream side effect depends on this proof.
        jlog.error("Claim %s granted without ownership for job %s - refusing to run unfenced",
                   claim.value, job_id)
        return DeliveryRefused("skipped", {"job_id": job_id, "status": "skipped",
                                           "reason": "no_ownership"})
    # Observability: the generation + a NON-SECRET token hash. The raw worker token is never
    # logged, traced, published, or put in the graph state.
    # INSTALL THE MIRROR before any execution authority is handed back. The PostgreSQL claim is
    # already committed; until Redis holds the matching fingerprint this worker may not publish,
    # so it may not start either. A failure here is fail-closed: no graph, no tool, no native run.
    from meshpipeline.contracts.event_stream import fence_fingerprint
    try:
        installed = install_execution_fence(
            job_id,
            fence_fingerprint(job_id, ownership.execution_generation, ownership.worker_token),
            ownership.fence_ttl_seconds or 1)
    except Exception as exc:  # noqa: BLE001
        installed = False
        jlog.error("Could not install the execution fence for job %s (%s) - refusing to run "
                   "with a claim Redis cannot enforce", job_id, type(exc).__name__)
    if not installed:
        return DeliveryRefused("skipped", {"job_id": job_id, "status": "skipped",
                                           "reason": "fence_unavailable"})
    jlog.info("Claimed execution - job_id=%s generation=%d backend=%s token=%s result=%s",
              job_id, ownership.execution_generation, backend, ownership.token_hash(), claim.value)
    return DeliveryClaim(ownership, worker_token,
                         getattr(existing, "created_at", None) if existing is not None else None)
