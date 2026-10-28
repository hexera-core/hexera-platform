# Responsibility: Remember when the running job last showed PROGRESS - new mesher output, a new step -
# so the clocks that could end it (application/pipeline_budget, the worker's wait for a remote mesh)
# end it only when it has stopped making progress, never while it is still working.
# Boundaries: process-local and engine-neutral. Whoever sees progress calls `touch` (the local
# watchdog in sandbox/safe_exec, the remote mesh's beacon relay in adapters/mesh_execution); the run
# resets it when it starts. Never raises.
from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

_LAST: list[float | None] = [None]
#: NATIVE TIME: seconds this run has spent inside native mesh runs (finished ones), and when the
#: one in flight started. The model loops' budgets are for MODEL time - a mesher that runs for an
#: hour, watched for progress, must not spend the builder loop's clock (contracts/native_time).
_NATIVE_DONE: list[float] = [0.0]
_NATIVE_SINCE: list[float | None] = [None]


#: A file the lease keeper (application/lease_keeper) reads: touched whenever the remote mesher this
#: job waits on is heard from, so a worker that is rightly idle - its mesh runs on another machine -
#: is never read as dead. Set by the keeper's start; None without a keeper.
_REMOTE_MARK: list[str | None] = [None]
#: ...and one touched on every progress, so the keeper can move the job's deadline on progress the
#: way the in-process beat does (application/pipeline_budget.progress_extension).
_PROGRESS_MARK: list[str | None] = [None]


def _mark(path: str | None) -> None:
    if not path:
        return
    try:
        import os
        with open(path, "a"):
            os.utime(path, None)
    except OSError:
        pass


def touch() -> None:
    """The job made progress just now."""
    _LAST[0] = time.monotonic()
    _mark(_PROGRESS_MARK[0])


def set_marks(*, remote: str | None, progress: str | None) -> None:
    _REMOTE_MARK[0], _PROGRESS_MARK[0] = remote, progress


def remote_heard() -> None:
    """The remote mesher this job waits on was heard from just now (its beacon was written again):
    the job is in flight on its machine. Never raises."""
    _mark(_REMOTE_MARK[0])


def seconds_since() -> float | None:
    """Seconds since the last progress touched; None when none was since the last reset."""
    last = _LAST[0]
    return None if last is None else max(0.0, time.monotonic() - last)


def reset() -> None:
    """A new run starts: progress another job made in this process says nothing about it."""
    _LAST[0] = None
    _NATIVE_DONE[0] = 0.0
    _NATIVE_SINCE[0] = None


@contextmanager
def native_run() -> Iterator[None]:
    """Count the time inside as NATIVE (a mesher running), not model time. Nested calls count once."""
    outer = _NATIVE_SINCE[0] is None
    if outer:
        _NATIVE_SINCE[0] = time.monotonic()
    try:
        yield
    finally:
        if outer and _NATIVE_SINCE[0] is not None:
            _NATIVE_DONE[0] += max(0.0, time.monotonic() - _NATIVE_SINCE[0])
            _NATIVE_SINCE[0] = None


def native_seconds() -> float:
    """Seconds this run has spent in native mesh runs so far, the one in flight included."""
    since = _NATIVE_SINCE[0]
    return _NATIVE_DONE[0] + (max(0.0, time.monotonic() - since) if since is not None else 0.0)


def native_running() -> bool:
    return _NATIVE_SINCE[0] is not None


def model_time() -> float:
    """A monotonic clock that STANDS STILL while a native mesher runs: what the model loops'
    budgets are measured on, so a long, progressing mesh never spends them."""
    return time.monotonic() - native_seconds()


__all__ = ["model_time", "native_run", "native_running", "native_seconds", "remote_heard", "reset",
           "seconds_since", "set_marks", "touch"]
