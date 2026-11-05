# Responsibility: Put a job back on the queue when its run died BEFORE it claimed the job, on a
# dependency that was only unavailable (the database full or restarting) - never leave it pending
# with no task anywhere.
# Owns: the decision (unclaimed + transient) and the re-launch with the run's own arguments.
# Boundaries: launches through the contracts/pipeline_execution seam, exactly as the API did; writes
# nothing to the database (which is what was unavailable). Collaborates with pipeline_run (the hook)
# and dispatch_contract (the payload).
"""dev-areen 25 Oct, Postgres full during a redeploy: a worker took a job's task, its first database
read failed with TooManyConnectionsError, and the crash finalize could not write either ("could not
finalize job via outbox: remaining connection slots..."). The task was dead-lettered, so the job row
stayed `pending` with no message on the queue - only the 4-hour never-started reaper would ever have
reached it. Eleven jobs.

A run that never claimed its job has done nothing: no lease, no generation, no workspace, no
side effect. Re-launching it is exactly the launch the API made, so the job simply waits in the
queue for the database to come back. The claim itself already waits a full database out
(execution_fence.claim_delivery_patiently, up to CLAIM_WAIT_SECONDS); this covers what is left - a
database still unavailable after that, or unavailable on another read before the claim.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The run's own arguments, set by pipeline_run.run_pipeline for the run it starts. A run entered
#: another way (a test calling _run_async directly) has none, and is never re-launched.
RUN_KWARGS: contextvars.ContextVar[dict | None] = contextvars.ContextVar("unclaimed_requeue_run_kwargs",
                                                                          default=None)

#: How long the worker waits before putting the job back: the database is unavailable right now,
#: and an immediate re-launch would only be taken and refused again at once.
REQUEUE_AFTER_S = 60.0

#: Said to the user while the job waits. Plain words, nothing for them to do.
REQUEUE_NOTE = ("Our database is busy for a moment, so your job is waiting in the queue. It will "
                "start by itself - nothing is needed from you.")


def transient(exc: BaseException) -> bool:
    """A dependency that was only unavailable: down, full, or answering slowly - not a defect."""
    from meshpipeline.errors import FailureClass, SystemFailure, classify_exception
    sf = exc if isinstance(exc, SystemFailure) else classify_exception(exc, "pipeline")
    return sf.failure_class in (FailureClass.DEPENDENCY_DOWN, FailureClass.PROVIDER_TRANSIENT)


async def requeue_if_unclaimed(job_id: str, exc: BaseException, *, claimed: bool, jlog: Any = logger,
                               sleep=asyncio.sleep) -> dict | None:
    """Re-launch the job when its run died before claiming it, on a transient dependency failure.
    Returns the task's result detail when it did; None when the crash path must run as before (the
    job was claimed, the failure is not transient, there are no run arguments, the launcher does not
    run work by itself, or the launch failed)."""
    if claimed or not transient(exc):
        return None
    kwargs = RUN_KWARGS.get()
    if not kwargs or kwargs.get("job_id") != job_id:
        return None
    from meshpipeline.contracts.pipeline_execution import get_pipeline_launcher, launcher_runs_work
    if not launcher_runs_work():
        return None
    from meshpipeline.application.dispatch_contract import build
    try:
        payload = build(**kwargs)
    except Exception as build_exc:  # noqa: BLE001 - an unrebuildable payload falls back to the crash path
        jlog.error("could not rebuild the dispatch payload of unclaimed job %s (%s)", job_id, build_exc)
        return None
    jlog.warning("Job %s died before it was claimed (%s: %s) - nothing was done yet, so it goes back "
                 "on the queue in %.0f s instead of failing", job_id, type(exc).__name__, exc,
                 REQUEUE_AFTER_S)
    try:
        from meshpipeline.application.maintenance.cleanup import publish_job_note
        publish_job_note(job_id, REQUEUE_NOTE, op_id="requeue:unclaimed")
    except Exception:  # noqa: BLE001 - the note is a courtesy
        pass
    await sleep(REQUEUE_AFTER_S)
    try:
        await get_pipeline_launcher().launch(None, job_id, payload)
    except Exception as launch_exc:  # noqa: BLE001 - the crash path then records what it can
        jlog.error("could not put unclaimed job %s back on the queue (%s)", job_id, launch_exc)
        return None
    # not a crash of this job: the delivery the redelivery cap counted is taken back
    from meshpipeline.contracts.delivery_guard import forgive_attempt
    forgive_attempt(job_id)
    return {"job_id": job_id, "status": "requeued", "reason": "unclaimed_dependency_unavailable"}


__all__ = ["REQUEUE_AFTER_S", "REQUEUE_NOTE", "RUN_KWARGS", "requeue_if_unclaimed", "transient"]
