# Responsibility: Declare how a big FEA sizing is sized on a machine of its own (TIME-21) - a neutral seam the geometry check calls and the runtime fills with the big-machine VM route - and how the check's watch on it is queued.
# Boundaries: contract only. No VM, no bucket, no queue: the runtime composes those (runtime/composition.py).
# Collaborates with: application/big_sizing.py (the caller), adapters/mesh_execution/fea_sizing_vm.py (the VM route), runtime/mesh_runner.py (runs SIZING_ENGINE).
from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: the `engine` a sizing is sent to the mesh container as (runtime/mesh_runner runs the sizing for it, not a mesher)
SIZING_ENGINE = "fea_sizing"


class SizingNotStarted(Exception):
    """No machine could be had (no quota, none free, no permission, not configured): the worker sizes it."""


class SizingRemote(Protocol):
    def start(self, ws: Path, *, key: str, timeout_s: int) -> dict:
        """Send the packed sizing in `ws` (engines/fea_plan_options.pack) to a machine of its own and return its
        handle (JSON: it rides on the check and in the watch's arguments). Raises SizingNotStarted when none
        could be had; nothing is left running then."""
        ...

    def look(self, handle: dict) -> dict:
        """{"state": "running" | "done" | "lost", "why": words}: done once its result is written; lost when its
        machine never reported or stopped answering. Never raises (unreadable is running)."""
        ...

    def collect(self, handle: dict, dest: Path) -> dict:
        """The sizing's result ({"rc", "fea_sizing": ...}), the files it sent back unpacked into `dest`."""
        ...

    def release(self, handle: dict, *, why: str) -> None:
        """Delete its machine and its exchange objects. Never raises; safe to call more than once."""
        ...


_remote: SizingRemote | None = None


def set_sizing_remote(remote: SizingRemote | None) -> None:
    global _remote
    _remote = remote


def sizing_remote() -> SizingRemote | None:
    """The remote route, or None when this process has none (the worker sizes everything)."""
    return _remote


WatchEnqueuer = Callable[..., Any]
"""Queues one look at a sizing away: enqueue_watch(session_id=..., token=..., handle=..., interpretation=...,
countdown=seconds)."""

_watch_enqueuer: WatchEnqueuer | None = None


def set_watch_enqueuer(enqueuer: WatchEnqueuer | None) -> None:
    global _watch_enqueuer
    _watch_enqueuer = enqueuer


def can_watch() -> bool:
    return _watch_enqueuer is not None


def enqueue_watch(**kwargs: Any) -> bool:
    """Queue the next look. False when nothing is configured to, or the queue refused it (the caller then gives
    the sizing back to the worker); never raises."""
    if _watch_enqueuer is None:
        return False
    try:
        _watch_enqueuer(**kwargs)
    except Exception as exc:  # noqa: BLE001 - not queued: the caller sizes it on the worker
        logger.warning("fea sizing watch not queued (%s) - session=%s", type(exc).__name__, kwargs.get("session_id"))
        return False
    return True


__all__ = ["SIZING_ENGINE", "SizingNotStarted", "SizingRemote", "WatchEnqueuer", "can_watch", "enqueue_watch",
           "set_sizing_remote", "set_watch_enqueuer", "sizing_remote"]
