# Responsibility: Satisfy the pipeline-execution contract for a deployment that runs no worker.
# Boundaries: it logs the command that will run the persisted payload and returns; it queues nothing.
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def launch(db, job_id: str, payload: dict) -> None:
    logger.info("pipeline payload persisted (deferred) - job_id=%s; run via "
                "`python -m meshpipeline.runtime.run_job --job-id %s`", job_id, job_id)
