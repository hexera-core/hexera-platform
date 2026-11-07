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
from meshpipeline.application.shell_check import SHELL_CHECK_LIMIT_S

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
    #
    # THE PLATFORM CONTRACT FIRST (application/platform_contract): a job the API stamped with a
    # contract this worker does not speak - the API rolled, the fleet did not yet - is held and put
    # back on the queue, never run into a TypeError or a schema it cannot read. Checked before
    # run_pipeline, so the job is not claimed and its delivery count is not touched.
    held = held_for_update(getattr(run_simulation, "request", None), kwargs)
    if held is not None:
        return held
    return run_pipeline(**kwargs)


def held_for_update(request, kwargs: dict) -> dict | None:
    """None when this worker may run the job; else the job was held and put back on the queue
    because it was stamped with a platform contract this worker does not speak."""
    from meshpipeline.application import platform_contract
    seen = platform_contract.message_contract(request)
    if not platform_contract.mismatch(seen):
        return None
    job_id = str(kwargs.get("job_id") or getattr(request, "id", "") or "")
    return platform_contract.hold_for_update(
        job_id, seen=seen,
        requeue=lambda: run_simulation.apply_async(kwargs=kwargs, task_id=job_id,
                                                   headers={platform_contract.HEADER: seen}))


# THE GEOMETRY TASKS ACK LATE TOO, for the pipeline task's reason (above). dev-areen, 2 Nov, six uploads at
# once on one worker's two check slots: acked early, each running check freed its prefetch slot at start,
# so the worker reserved the next two checks and sat on them - out of the queue list (the console's line
# could only say "Waiting to start…", never "Queued behind N"), out of the demand the autoscaler reads, and
# out of reach of any machine it would add. Acked late, the two running checks hold the two slots and the
# rest wait in the list, where they are counted, said and taken by the next free slot on any machine.
@celery_app.task(
    name="worker.tasks.scout_geometry",
    bind=False,
    max_retries=0,
    acks_late=True,
    # the application's numbers: the API reads a check through a time box built from them
    soft_time_limit=SCOUT_SOFT_LIMIT_S,
    time_limit=SCOUT_HARD_LIMIT_S,
)
def scout_geometry(**kwargs) -> dict:
    # THE GEOMETRY CHECK runs on the worker because reading CAD and drawing it need the mesh
    # toolchain the API image does not carry. It runs on the `geometry_checks` queue (celery_app.py
    # task_routes), which every worker drains from a slot of its own, so an upload is drawn while a
    # mesh job runs beside it rather than after it.
    from meshpipeline.adapters.pipeline_execution.geometry_running import running
    from meshpipeline.application.geometry_check import run_geometry_check
    # COUNTED WHILE IT RUNS (geometry_running): the fleet's demand sees checks in flight, not only queued
    with running(_task_id(scout_geometry, f"geometry-check-{kwargs.get('session_id', '')}")):
        return run_geometry_check(**kwargs)


@celery_app.task(
    name="worker.tasks.name_geometry",
    bind=False,
    max_retries=0,
    acks_late=True,
    soft_time_limit=NAMING_SOFT_LIMIT_S,
    time_limit=NAMING_HARD_LIMIT_S,
)
def name_geometry(**kwargs) -> dict:
    # THE NAMING runs once the user has said what the part is: the pictures the scout stored and
    # the user's words go to the vision model together. Same queue as the scout - the user is
    # waiting on this one too.
    from meshpipeline.adapters.pipeline_execution.geometry_running import running, waiting
    from meshpipeline.application.geometry_check import run_geometry_naming, scout_still_running
    yielded = int(kwargs.pop(NAMING_YIELDED, 0) or 0)
    task_id = _task_id(name_geometry, f"geometry-naming-{kwargs.get('session_id', '')}")
    # A NAMING THAT COULD ONLY WAIT GIVES ITS SLOT TO A TASK THAT IS WAITING (shared dev, 2 Nov: the stud
    # punch's scout and its naming - waiting on that scout - held both of the worker's two slots for four
    # minutes, while the Camaro's naming, a half-minute model call on a part already scouted, sat queued
    # behind them until the user gave up on it). Its own scout still at work and another task in the queue,
    # it goes back to the end of the queue and the slot takes the next task; bounded, so it never starves.
    if yielded < NAMING_YIELD_MAX and scout_still_running(str(kwargs.get("session_id") or "")) and waiting() > 0:
        from meshpipeline.application.geometry_check import naming_limits_for
        soft, hard = naming_limits_for(str(kwargs.get("session_id") or ""))
        name_geometry.apply_async(kwargs={**kwargs, NAMING_YIELDED: yielded + 1}, task_id=task_id,
                                  soft_time_limit=soft, time_limit=hard)
        logger.info("geometry naming: its scout is still running and a task is waiting - back in the queue "
                    "(%d) - session=%s", yielded + 1, kwargs.get("session_id"))
        return {"status": "yielded", "yielded": yielded + 1}
    with running(task_id):
        return run_geometry_naming(**kwargs)


@celery_app.task(
    name="worker.tasks.read_part_words",
    bind=False,
    max_retries=0,
    acks_late=True,
    # the vision model's own limit is 3 minutes (cad/part_words.TIMEOUT_S); the pictures and the store besides
    soft_time_limit=300,
    time_limit=360,
)
def read_part_words(**kwargs) -> dict:
    # THE PARTS CHECKLIST BEFORE APPROVAL (application/part_matches): the user's words about parts tied to the
    # check's parts by the vision model, while the user waits on the card. The geometry queue: the same worker
    # slot as the naming, the same toolchain-free model call.
    from meshpipeline.adapters.pipeline_execution.geometry_running import running
    from meshpipeline.application.part_matches import run_reading
    with running(_task_id(read_part_words, f"part-words-{kwargs.get('session_id', '')}")):
        return run_reading(**kwargs)


#: the naming's yields so far (a task argument the naming body never sees), and how many it may make: each
#: one lets a waiting task run first, so a busy queue can never keep a naming from its turn for long
NAMING_YIELDED = "naming_yielded"
NAMING_YIELD_MAX = 12


def _task_id(task, fallback: str) -> str:
    request = getattr(task, "request", None)
    return str(getattr(request, "id", None) or fallback)


@celery_app.task(
    name="worker.tasks.shell_check_geometry",
    bind=False,
    max_retries=0,
    soft_time_limit=SHELL_CHECK_LIMIT_S + 120,
    time_limit=SHELL_CHECK_LIMIT_S + 180,
)
def shell_check_geometry(**kwargs) -> dict:
    # SHSTAGE-2: AN FEA PART'S SHELL CHECK, started once the part is measured: its mid-surface found and
    # drawn for the stage's "Mesh as" choice. Same queue as the scout and the naming (the user is on the
    # stage); the extraction runs in a child process with its own time limit (application/shell_check).
    from meshpipeline.application.shell_check import run_shell_check
    return run_shell_check(**kwargs)


async def launch(db, job_id: str, payload: dict) -> None:
    # Strip the envelope (schema_version) and fill gaps HERE, exactly as the deferred backend
    # does via run_from_job → to_run_kwargs. run_pipeline takes no **kwargs, so passing the
    # raw payload (which carries schema_version) would make run_simulation(**payload) raise
    # TypeError inside the worker - after the API already told the user meshing started. Both
    # backends must reconstruct run kwargs through the ONE contract, never splat the raw payload.
    from meshpipeline.application import platform_contract
    from meshpipeline.application.dispatch_contract import to_run_kwargs
    # stamped with the run contract this API speaks: a worker that speaks another holds the job
    # until one that matches takes it (application/platform_contract)
    run_simulation.apply_async(kwargs=to_run_kwargs(payload, where=f"celery launch job {job_id}"),
                               task_id=job_id, headers=platform_contract.headers())
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
