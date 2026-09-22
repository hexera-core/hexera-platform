# Responsibility: Declare how the geometry check is started for an upload - a neutral enqueue seam
# the API calls and the runtime fills with the worker adapter.
# Boundaries: contract only. No queue, no broker, no task; the runtime composes those.
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

ScoutEnqueuer = Callable[..., Any]
"""Starts one geometry check: enqueue_scout(session_id=..., owner_id=..., source=..., interpretation=...)."""

_enqueuer: ScoutEnqueuer | None = None


def set_scout_enqueuer(enqueuer: ScoutEnqueuer | None) -> None:
    global _enqueuer
    _enqueuer = enqueuer


def enqueue_scout(**kwargs: Any) -> bool:
    """Hand the check to whoever runs it. False when nothing is configured to, so the caller can
    say so instead of waiting for a picture that will never come."""
    if _enqueuer is None:
        logger.info("geometry check not configured - skipping enqueue for session=%s",
                    kwargs.get("session_id"))
        return False
    _enqueuer(**kwargs)
    return True
