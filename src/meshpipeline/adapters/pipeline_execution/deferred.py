# Responsibility: Satisfy the pipeline-execution contract for a deployment that runs no worker.
# Boundaries: it logs the command that will run the persisted payload and returns; it queues nothing.
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def launch(db, job_id: str, payload: dict) -> None:
    logger.info("pipeline payload persisted (deferred) - job_id=%s; run via "
                "`python -m meshpipeline.runtime.run_job --job-id %s`", job_id, job_id)


async def revoke(job_id: str) -> None:
    # Nothing was queued, so there is nothing to withdraw. The persisted payload stays with the
    # job; a later `run_job` of a cancelled job is refused at its claim as already terminal.
    logger.info("no launch to revoke (deferred) - job_id=%s", job_id)
