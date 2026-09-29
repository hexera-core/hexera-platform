# Responsibility: Keep a job alive when the worker running it is shut down on purpose - stop the run, give the job back to the queue, tell the user.
# Owns: the drain watch around a graph run, the hand-back sequence, and the one re-launch helper the reaper shares.
# Boundaries: it hands back; claiming the job again is the next worker's ordinary claim (persistence/lease.py).
# Collaborates with: contracts/worker_drain.py, persistence/lease.py, application/maintenance/cleanup.py.
"""A worker going away hands its job back instead of taking it down with it.

WHAT HAPPENED WITHOUT THIS. Shared dev, 2026-09-29: the fleet's autoscaler scaled in three times
in two hours (05:41, 06:39, 07:32 UTC), each time deleting VMs that were in the middle of a mesh
job. A VM goes down in under a minute; the job's lease stopped being renewed; the stalled-job
reaper failed it about thirty minutes later as `worker_lost` and told the user to say "run it
again". Three of the six runs that got past the intake that morning ended that way.

WHAT HAPPENS NOW. Deleting a worker VM runs its shutdown script, which stops the worker container;
celery's main process gets SIGTERM, stops taking work, and marks the worker as draining
(runtime/worker_drain.py). The run polls that mark while its graph runs (`run_unless_draining`).
On a yes it cancels the graph, gives the job back (`hand_back`: lease released, status back to
`pending`, a fresh message on the queue) and returns. The next free worker claims it as a new
execution generation and runs it from the start; the user is told the job moved and needs nothing
from them. All of it inside the ~90 seconds a VM being deleted gets.

WHY FROM THE START, NOT FROM THE CHECKPOINT. The LangGraph checkpoint is durable, but the attempt
it points into is not: the builder's case files and the mesh the executor checks live in the
attempt workspace on the dying VM's own disk (agents/builder/workspace.py). A checkpoint resumed
on another machine past the builder would find no mesh and fail with a cause that is not true
("the mesher stopped before it finished"). A new generation is the takeover the lease model
already proves safe; resuming mid-run needs the attempt workspace in durable storage first.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from typing import Any, TypeVar

from meshpipeline.contracts.worker_drain import drain_requested

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: How often a running graph looks for the drain mark. A VM being deleted gets about ninety
#: seconds in all, and the hand-back itself takes a few, so a second of latency is cheap.
DRAIN_POLL_SECONDS = 1.0
#: How long the cancelled graph gets to unwind before the job is handed back regardless. The
#: hand-back fences the old execution, so anything still unwinding after it can write nothing.
CANCEL_GRACE_SECONDS = 15.0

#: Said to the user the moment the job leaves the machine. Plain words: what happened, that it is
#: handled, and that nothing is needed from them.
HANDOFF_NOTE = ("The machine running this job is being shut down, so the job is moving to another "
                "machine and will start again there. Nothing is needed from you.")


class WorkerDraining(Exception):
    """The worker is shutting down and the run was stopped so its job can be handed back.

    Raised out of the graph run and caught by the orchestrator BEFORE its crash handler: this is
    not a failure, and nothing terminal may be recorded for it."""


async def _pause(seconds: float) -> None:
    # A timer on the loop itself, not `asyncio.sleep`: this runs beside every graph, and a sleep
    # that does not actually wait - a test replaces `asyncio.sleep` to skip provider backoff, and
    # so could anything else - would turn the watch into a loop that never yields, and the graph
    # it is watching would never run again.
    loop = asyncio.get_running_loop()
    done: asyncio.Future[None] = loop.create_future()

    def _ring() -> None:
        if not done.done():
            done.set_result(None)

    timer = loop.call_later(max(0.0, seconds), _ring)
    try:
        await done
    finally:
        timer.cancel()


async def _wait_for_drain(poll_seconds: float) -> None:
    while not drain_requested():
        await _pause(poll_seconds)


async def run_unless_draining(work: Awaitable[T], *, poll_seconds: float = DRAIN_POLL_SECONDS,
                              cancel_grace_seconds: float = CANCEL_GRACE_SECONDS) -> T:
    """Await `work` - unless this worker starts draining first. Then cancel it, give it a short
    grace to unwind, and raise WorkerDraining.

    A run that finished before the drain was seen keeps its own outcome: a finished mesh is
    delivered, not handed back to be built again. Once the cancel is sent, whatever comes back is
    handed back regardless - a graph step that swallowed its cancellation and returned would hand
    the finalizer a half-run state, and a verdict derived from that would be false. Running the job
    again elsewhere costs time; finalizing half a run costs the truth."""
    task: asyncio.Future[T] = asyncio.ensure_future(work)
    watch: asyncio.Future[Any] = asyncio.ensure_future(_wait_for_drain(poll_seconds))
    racing: set[asyncio.Future[Any]] = {task, watch}
    try:
        await asyncio.wait(racing, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        task.cancel()
        raise
    finally:
        watch.cancel()
    if task.done():
        return task.result()
    logger.warning("worker is draining - stopping the run so its job can be handed back")
    task.cancel()
    await asyncio.wait({task}, timeout=cancel_grace_seconds)
    if task.done() and not task.cancelled():
        task.exception()          # retrieved, so an unwinding error is not reported as unhandled
    raise WorkerDraining("the worker is shutting down; the job is being handed back")


async def relaunch(db, job_id: str, payload: dict | None, *, log: Any = logger) -> bool:
    """Put a job back on the queue with its approved dispatch payload. True when the launcher took
    it. Shared by the hand-back and the reaper's re-run, so both re-launch exactly the way the API
    launched the run in the first place (application/pipeline_run.dispatch)."""
    if not payload:
        log.error("cannot re-launch job %s: it has no dispatch payload", job_id)
        return False
    from meshpipeline.contracts.pipeline_execution import get_pipeline_launcher
    try:
        await get_pipeline_launcher().launch(db, job_id, payload)
    except Exception as exc:  # noqa: BLE001 - the caller decides what an unlaunched job becomes
        log.error("could not re-launch job %s (%s: %s)", job_id, type(exc).__name__, exc)
        return False
    return True


def _announce(ownership, job_id: str) -> None:
    # Through the same channel the reaper's re-run speaks on: the hand-back is giving up the
    # job's ownership, not acting as its run, so it says so as the maintenance authority does.
    # Best effort: a note that cannot be said never blocks the hand-back, which is the part the
    # user actually needs.
    from meshpipeline.application.maintenance.cleanup import publish_job_note
    publish_job_note(job_id, HANDOFF_NOTE, op_id=f"handoff:g{ownership.execution_generation}")


async def hand_back(session_factory, ownership, *, jlog: Any = logger, lease_repo=None,
                    job_repo=None) -> dict:
    """Give the job this worker owns back to the queue. Returns the task's result detail.

    1. Tell the user the job is moving to another machine.
    2. Release it (LeaseRepository.hand_back): lease and fence gone, status back to `pending`.
       Refused when the job was cancelled or taken over meanwhile - then there is nothing to give.
    3. Take back this run's delivery count: a planned move is not a crash (contracts/delivery_guard).
    4. Re-launch it. If that fails, put it where the reaper's automatic re-run finds it, rather
       than leave a pending job with no message that nothing would ever run.
    """
    from meshpipeline.contracts.delivery_guard import forgive_attempt
    from meshpipeline.persistence.lease import LeaseRepository
    from meshpipeline.persistence.repositories.job_repository import JobRepository

    job_id = str(ownership.job_id)
    leases = lease_repo or LeaseRepository()
    jobs = job_repo or JobRepository()
    _announce(ownership, job_id)
    try:
        async with session_factory() as db:
            released = await leases.hand_back(db, ownership)
            payload = await jobs.get_dispatch_payload(db, ownership.job_id) if released else None
            await db.commit()
    except Exception as exc:  # noqa: BLE001 - nothing was released; the lease lapses on its own
        jlog.error("could not hand job %s back (%s) - the reaper re-runs it once its lease lapses",
                   job_id, type(exc).__name__)
        return {"job_id": job_id, "status": "handback_failed"}
    if not released:
        jlog.warning("nothing to hand back for job %s - it was cancelled or another worker owns "
                     "it now", job_id)
        return {"job_id": job_id, "status": "fenced", "skipped": "not_owner"}
    forgive_attempt(job_id)

    async with session_factory() as db:
        launched = await relaunch(db, job_id, payload, log=jlog)
    if launched:
        jlog.warning("job %s handed back to the queue (generation %d) - the next free worker "
                     "runs it", job_id, ownership.execution_generation)
        return {"job_id": job_id, "status": "handed_back"}

    try:
        async with session_factory() as db:
            await leases.unqueue_hand_back(db, ownership)
            await db.commit()
    except Exception as exc:  # noqa: BLE001 - logged; the job's deadline still bounds it
        jlog.error("job %s was released but neither re-queued nor returned to the reaper (%s) - "
                   "it waits until its pipeline deadline", job_id, type(exc).__name__)
        return {"job_id": job_id, "status": "handback_unqueued"}
    jlog.error("job %s could not be re-queued - left for the reaper's automatic re-run", job_id)
    return {"job_id": job_id, "status": "handback_unqueued"}


__all__ = ["CANCEL_GRACE_SECONDS", "DRAIN_POLL_SECONDS", "HANDOFF_NOTE", "WorkerDraining",
           "hand_back", "relaunch", "run_unless_draining"]
