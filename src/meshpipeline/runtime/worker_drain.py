# Responsibility: Carry "this worker is shutting down" from the celery main process to the pool process running the job.
# Owns: the drain marker - where it lives, who writes it, who reads it, and when it is cleared.
# Boundaries: it signals; handing the job back is application/worker_handoff.py.
"""The drain marker.

WHY A FILE. Docker delivers SIGTERM to PID 1, which is celery's main process (the image's
entrypoint `exec`s it). The job runs in a prefork POOL process that is told nothing: a warm
shutdown simply waits for it to finish, which for a mesh job is longer than any VM shutdown
lasts. The main process learns first, through celery's `worker_shutting_down` signal, and writes a
marker; the pool process polls for it between graph steps and hands its job back.

A file in the container's own temp directory is the smallest channel both processes already
share. It is named after the MAIN process's pid, so two workers on one machine (a developer
running two, or the fleet's geometry worker beside the simulation worker - each in its own
container anyway) never drain each other, and a pool process finds its parent's marker by
`os.getppid()`.

CLEARED AT START. A container restarted in place (`docker compose restart`, `--restart always`)
keeps its filesystem, and its celery main process is PID 1 again - the same name. A marker left by
the previous run would make every job the new worker takes hand itself straight back. So the main
process removes its own marker before it forks a single pool process.
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_MARKER_PREFIX = "hexera-worker-draining."


def marker_path(pid: int) -> Path:
    return Path(tempfile.gettempdir()) / f"{_MARKER_PREFIX}{int(pid)}"


def request_drain(pid: int | None = None) -> Path:
    """Mark the worker whose main process is `pid` (default: this process) as shutting down."""
    path = marker_path(os.getpid() if pid is None else pid)
    try:
        path.write_text("draining\n", encoding="utf-8")
    except OSError as exc:
        # Nothing to fall back to: the job then ends as it always did, by its lease lapsing and
        # the reaper re-running it. Said loudly, because that is thirty minutes of the user's time.
        logger.error("could not write the drain marker %s (%s) - the running job will not be "
                     "handed back; the reaper re-runs it once its lease lapses", path, exc)
    else:
        logger.warning("worker is shutting down - the running job will be handed back to the "
                       "queue (marker %s)", path)
    return path


def clear_drain(pid: int | None = None) -> None:
    try:
        marker_path(os.getpid() if pid is None else pid).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("could not clear a stale drain marker (%s)", exc)


def drain_requested() -> bool:
    """The probe a pool process installs: has its main process - or this process itself, when the
    job runs in the main process (the solo pool) - begun shutting down?"""
    return marker_path(os.getppid()).exists() or marker_path(os.getpid()).exists()


__all__ = ["clear_drain", "drain_requested", "marker_path", "request_drain"]
