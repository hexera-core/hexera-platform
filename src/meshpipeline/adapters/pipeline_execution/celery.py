# Responsibility: Launch a pipeline run as a Celery task, and run it when the task arrives.
# Boundaries: the queue seam; the run itself is application/pipeline_run.py.
from __future__ import annotations

import asyncio
import logging

from meshpipeline.adapters.pipeline_execution.celery_app import celery_app
from meshpipeline.application.geometry_check import (
    NAMING_HARD_LIMIT_S,
    NAMING_SOFT_LIMIT_S,
    SCOUT_HARD_LIMIT_S,
    SCOUT_SOFT_LIMIT_S,
)
from meshpipeline.application.pipeline_run import run_pipeline

logger = logging.getLogger(__name__)

#: A launch here is a message on the simulation queue, which a worker takes with nobody's help -
#: so the stalled-job reaper may re-run a job through it (contracts/pipeline_execution).
RUNS_WORK = True


# The task identifier is PRESERVED deliberately: "worker.tasks.run_simulation" is the on-the-wire
# name already enqueued on the broker queue, so renaming it for tidiness would orphan in-flight jobs
# (a worker would never pick up a message addressed to the old name). This is compatibility, not cruft.
#
# ACKNOWLEDGED LATE - this task only, and why that is not the redelivery the rest of the app avoids.
# Celery acks an early-acked task when it STARTS, which frees the worker's one prefetch slot, so a
# worker busy with a mesh job immediately reserved the NEXT job and sat on it: invisible to the
# queue-depth metric (it is no longer in the list), invisible to every idle worker, and waiting
# behind a run that can take hours. When the busy worker's VM was then deleted, the reserved job
# stayed in Redis's unacked set until the broker's one-hour visibility timeout put it back. That is
# the hour two jobs spent "queued" on shared dev on 2026-09-29 (created 05:24, started 06:27;
# created 06:27, started 07:28). Acked late, the running job holds the slot and nothing else is
# reserved. The redelivery the early ack was chosen to prevent - a long job re-run from scratch
# because it outran the visibility window - cannot happen: celery_app.py sets that window above the
# pipeline's whole deadline. A job whose worker dies is re-run by the reaper, once, not by the broker.
@celery_app.task(
    name="worker.tasks.run_simulation",
    bind=False,
    max_retries=0,
    acks_late=True,
    soft_time_limit=86400,
    time_limit=90000,
)
def run_simulation(**kwargs) -> dict:
    # Thin Celery boundary - the worker entrypoint (runtime/celery_worker) has already
    # composed every adapter for this fork; the run itself is the neutral application use case.
    return run_pipeline(**kwargs)


@celery_app.task(
    name="worker.tasks.scout_geometry",
    bind=False,
    max_retries=0,
    # the application's numbers: the API reads a check through a time box built from them
    soft_time_limit=SCOUT_SOFT_LIMIT_S,
    time_limit=SCOUT_HARD_LIMIT_S,
)
def scout_geometry(**kwargs) -> dict:
    # THE GEOMETRY CHECK runs on the worker because reading CAD and drawing it need the mesh
    # toolchain the API image does not carry. It runs on the `geometry_checks` queue (celery_app.py
    # task_routes), which every worker drains from a slot of its own, so an upload is drawn while a
    # mesh job runs beside it rather than after it.
    from meshpipeline.application.geometry_check import run_geometry_check
    return run_geometry_check(**kwargs)


@celery_app.task(
    name="worker.tasks.name_geometry",
    bind=False,
    max_retries=0,
    soft_time_limit=NAMING_SOFT_LIMIT_S,
    time_limit=NAMING_HARD_LIMIT_S,
)
def name_geometry(**kwargs) -> dict:
    # THE NAMING runs once the user has said what the part is: the pictures the scout stored and
    # the user's words go to the vision model together. Same queue as the scout - the user is
    # waiting on this one too.
    from meshpipeline.application.geometry_check import run_geometry_naming
    return run_geometry_naming(**kwargs)


async def launch(db, job_id: str, payload: dict) -> None:
    # Strip the envelope (schema_version) and fill gaps HERE, exactly as the deferred backend
    # does via run_from_job → to_run_kwargs. run_pipeline takes no **kwargs, so passing the
    # raw payload (which carries schema_version) would make run_simulation(**payload) raise
    # TypeError inside the worker - after the API already told the user meshing started. Both
    # backends must reconstruct run kwargs through the ONE contract, never splat the raw payload.
    from meshpipeline.application.dispatch_contract import to_run_kwargs
    run_simulation.apply_async(kwargs=to_run_kwargs(payload, where=f"celery launch job {job_id}"),
                               task_id=job_id)
    logger.info("pipeline dispatched via celery - job_id=%s", job_id)


async def revoke(job_id: str) -> None:
    # The task id IS the job id (see `launch`), so the broadcast names exactly one task. Without
    # `terminate`: a task still on the queue is discarded when a worker reaches it; one already
    # running is left to the execution fence, which refuses its result. The revoked set lives in
    # worker memory, so a worker restarted in between may still take the message - and then
    # claim_delivery refuses it as already terminal. Two guards, either one sufficient.
    # Off the event loop: the broadcast talks to the broker, and a slow broker must not stall the
    # request that already made the cancel durable.
    await asyncio.to_thread(celery_app.control.revoke, job_id)
    logger.info("pipeline launch revoked via celery - job_id=%s", job_id)
