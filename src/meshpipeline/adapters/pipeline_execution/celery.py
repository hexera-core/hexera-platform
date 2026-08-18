# Responsibility: Launch a pipeline run as a Celery task, and run it when the task arrives.
# Boundaries: the queue seam; the run itself is application/pipeline_run.py.
from __future__ import annotations

import logging

from meshpipeline.adapters.pipeline_execution.celery_app import celery_app
from meshpipeline.application.pipeline_run import run_pipeline

logger = logging.getLogger(__name__)


# The task identifier is PRESERVED deliberately: "worker.tasks.run_simulation" is the on-the-wire
# name already enqueued on the broker queue, so renaming it for tidiness would orphan in-flight jobs
# (a worker would never pick up a message addressed to the old name). This is compatibility, not cruft.
@celery_app.task(
    name="worker.tasks.run_simulation",
    bind=False,
    max_retries=0,
    soft_time_limit=86400,
    time_limit=90000,
)
def run_simulation(**kwargs) -> dict:
    # Thin Celery boundary - the worker entrypoint (runtime/celery_worker) has already
    # composed every adapter for this fork; the run itself is the neutral application use case.
    return run_pipeline(**kwargs)


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
