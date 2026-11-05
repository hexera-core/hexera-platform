# Responsibility: Let a running job ask whether the worker it runs on is shutting down.
# Owns: the drain probe binding, and the rule that an unanswerable question reads as "no".
# Boundaries: a yes/no question only; the runtime decides how the answer is known, the application what to do with it.
"""Is this worker going away?

A worker VM is deleted on purpose all the time: the autoscaler scales in, a fleet deploy rolls the
group onto a new template, an operator removes an instance from the console. Until this seam
existed nothing told the job running there. The VM went down in under a minute, the job's lease
stopped being renewed, and the stalled-job reaper failed it half an hour later as "worker lost" -
three of six runs on shared dev on 2026-09-29, all to autoscaler scale-in.

The runtime installs a probe in each worker process (runtime/celery_worker.py) that answers from
the worker's own shutdown signal; the pipeline run polls it while the graph runs and, on a yes,
hands its job back to the queue (application/worker_handoff.py). Anything that is not a celery
worker - the API, a one-shot `run_job`, a test - installs nothing, and the answer is always no.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)

DrainProbe = Callable[[], bool]

_probe: DrainProbe | None = None


def set_drain_probe(probe: DrainProbe | None) -> None:
    global _probe
    _probe = probe


def drain_requested() -> bool:
    """True once the worker this process belongs to has begun shutting down.

    Never raises. A probe that cannot answer reads as "not draining": the cost of a wrong no is
    the old behaviour (the reaper re-runs the job once its lease lapses), while a wrong yes would
    hand back a healthy run on a worker that is staying up."""
    if _probe is None:
        return False
    try:
        return bool(_probe())
    except Exception as exc:  # noqa: BLE001 - see the docstring: an unanswerable probe is a no
        logger.warning("worker drain probe failed (%s) - treating the worker as staying up",
                       type(exc).__name__)
        return False


__all__ = ["DrainProbe", "drain_requested", "set_drain_probe"]
