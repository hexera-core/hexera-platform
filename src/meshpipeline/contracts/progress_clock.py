# Responsibility: Remember when the running job last showed PROGRESS - new mesher output, a new step -
# so the clocks that could end it (application/pipeline_budget, the worker's wait for a remote mesh)
# end it only when it has stopped making progress, never while it is still working.
# Boundaries: process-local and engine-neutral. Whoever sees progress calls `touch` (the local
# watchdog in sandbox/safe_exec, the remote mesh's beacon relay in adapters/mesh_execution); the run
# resets it when it starts. Never raises.
from __future__ import annotations

import time

_LAST: list[float | None] = [None]


def touch() -> None:
    """The job made progress just now."""
    _LAST[0] = time.monotonic()


def seconds_since() -> float | None:
    """Seconds since the last progress touched; None when none was since the last reset."""
    last = _LAST[0]
    return None if last is None else max(0.0, time.monotonic() - last)


def reset() -> None:
    """A new run starts: progress another job made in this process says nothing about it."""
    _LAST[0] = None


__all__ = ["reset", "seconds_since", "touch"]
