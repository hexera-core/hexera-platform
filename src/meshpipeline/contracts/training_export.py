# Responsibility: Declare how a finished run is handed to training export.
# Boundaries: a one-call seam so the pipeline never imports the exporter.
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_enqueuer = None


def set_export_enqueuer(enqueuer) -> None:
    global _enqueuer
    _enqueuer = enqueuer


def enqueue_export(job_id: str, state: dict, *, created_at: Any = None, ended_at: Any = None) -> None:
    if _enqueuer is None:
        logger.info("training export not configured - skipping enqueue for job_id=%s", job_id)
        return
    _enqueuer(job_id, state, created_at=created_at, ended_at=ended_at)
