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
#: THE SAME MOMENT ON THE WALL CLOCK, because the monotonic one does not survive a suspend.
#: On Linux CLOCK_MONOTONIC does not advance while the system is suspended (CLOCK_BOOTTIME does),
#: so a host that slept for ten hours wakes believing it worked continuously. MEASURED on job
#: 4860d75f, 2026-11-15/16: the laptop running the stack slept 40 minutes and then 10 h 28 min -
#: proved by celery beat, an independent container with restarts=0, missing a five-minute cron
#: across both holes - and the age below would have read as seconds. `progress_extension` turns a
#: small age into a deadline PROGRESS_GRACE_S in the future, so sleeping through the night would
#: have bought the run a 17-minute extension it had not earned.
_LAST_WALL: list[float | None] = [None]
#: NATIVE TIME: seconds this run has spent inside native mesh runs (finished ones), and when the
#: one in flight started. The model loops' budgets are for MODEL time - a mesher that runs for an
#: hour, watched for progress, must not spend the builder loop's clock (contracts/native_time).
_NATIVE_DONE: list[float] = [0.0]
_NATIVE_SINCE: list[float | None] = [None]


def touch() -> None:
    """The job made progress just now."""
    _LAST[0] = time.monotonic()
    _LAST_WALL[0] = time.time()


def seconds_since() -> float | None:
    """Seconds since the last progress touched; None when none was since the last reset.

    THE OLDER OF THE TWO CLOCKS, never the younger. The monotonic one is right about elapsed work
    and immune to a clock adjustment; the wall one is the only one that notices a suspend. Taking
    the larger age means a step backwards in wall time (NTP, a timezone fix) cannot make a run
    look stale, and a suspend cannot make a frozen run look fresh.
    """
    last = _LAST[0]
    if last is None:
        return None
    age = max(0.0, time.monotonic() - last)
    wall = _LAST_WALL[0]
    if wall is not None:
        age = max(age, max(0.0, time.time() - wall))
    return age


def reset() -> None:
    """A new run starts: progress another job made in this process says nothing about it."""
    _LAST[0] = None
    _LAST_WALL[0] = None
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


__all__ = ["model_time", "native_run", "native_running", "native_seconds", "reset", "seconds_since",
           "touch"]
