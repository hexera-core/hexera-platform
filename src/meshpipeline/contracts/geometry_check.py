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


ShellCheckEnqueuer = Callable[..., Any]
"""Starts one shell check (SHSTAGE-2): enqueue_shell_check(session_id=..., owner_id=..., interpretation=...)."""

_shell_enqueuer: ShellCheckEnqueuer | None = None


def set_shell_check_enqueuer(enqueuer: ShellCheckEnqueuer | None) -> None:
    global _shell_enqueuer
    _shell_enqueuer = enqueuer


def enqueue_shell_check(**kwargs: Any) -> bool:
    """Hand an FEA part's shell check to whoever runs it. False when nothing is configured to, so the
    stage says the walls were not measured instead of measuring forever."""
    if _shell_enqueuer is None:
        logger.info("shell check not configured - skipping enqueue for session=%s", kwargs.get("session_id"))
        return False
    _shell_enqueuer(**kwargs)
    return True


QueueReader = Callable[[str], Any]
"""Where one geometry task stands: reader(task_id) -> {"ahead": N} while it waits in the queue (N checks will
be taken before it), {"running": True} once a worker has it, or None when the queue cannot say."""

_queue_reader: QueueReader | None = None


def set_queue_reader(reader: QueueReader | None) -> None:
    global _queue_reader
    _queue_reader = reader


def queue_state(task_id: str) -> dict | None:
    """Where the task stands in the queue (QueueReader); None when nothing is configured to say, or it
    could not. Never raises: the live line is said without it."""
    if _queue_reader is None:
        return None
    try:
        got = _queue_reader(task_id)
    except Exception as exc:  # noqa: BLE001 - the line says less, never fails
        logger.info("geometry queue state unavailable (%s) for %s", type(exc).__name__, task_id)
        return None
    return got if isinstance(got, dict) else None
