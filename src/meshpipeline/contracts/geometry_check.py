# Responsibility: Declare how the geometry check is started for an upload - a neutral enqueue seam
# the API calls and the runtime fills with the worker adapter.
# Boundaries: contract only. No queue, no broker, no task; the runtime composes those.
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

ScoutEnqueuer = Callable[..., Any]
"""Starts one geometry scout: enqueue_scout(session_id=..., owner_id=..., source=..., interpretation=...)."""
NamingEnqueuer = Callable[..., Any]
"""Starts one naming: enqueue_naming(session_id=..., owner_id=..., purpose_text=..., interpretation=...)."""

_enqueuer: ScoutEnqueuer | None = None
_naming_enqueuer: NamingEnqueuer | None = None


def set_scout_enqueuer(enqueuer: ScoutEnqueuer | None) -> None:
    global _enqueuer
    _enqueuer = enqueuer


def set_naming_enqueuer(enqueuer: NamingEnqueuer | None) -> None:
    global _naming_enqueuer
    _naming_enqueuer = enqueuer


def enqueue_scout(**kwargs: Any) -> bool:
    """Hand the scout to whoever runs it. False when nothing is configured to, so the caller can
    say so instead of waiting for a picture that will never come."""
    if _enqueuer is None:
        logger.info("geometry check not configured - skipping enqueue for session=%s",
                    kwargs.get("session_id"))
        return False
    _enqueuer(**kwargs)
    return True


def enqueue_naming(**kwargs: Any) -> bool:
    """Hand the user's words and the pictures to the naming step. False when nothing is
    configured to run it, so the chat carries on asking instead of holding for nothing."""
    if _naming_enqueuer is None:
        logger.info("geometry naming not configured - skipping enqueue for session=%s",
                    kwargs.get("session_id"))
        return False
    _naming_enqueuer(**kwargs)
    return True
