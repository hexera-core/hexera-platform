# Responsibility: Own the run's one wall-clock ceiling and hand out what remains of it.
# Owns: the deadline anchored once at execution start, remaining time, and the cap on every child budget.
# Boundaries: the deadline is never reset, so a retry cannot extend a run by restarting its clock.
from __future__ import annotations

import time
from datetime import datetime
from typing import NamedTuple

import meshpipeline.settings.runtime as rtcfg


def deadline_epoch_from_created_at(created_at: datetime | None) -> float:
    base = created_at.timestamp() if created_at is not None else time.time()
    return base + float(rtcfg.PIPELINE_TOTAL_TIMEOUT_SECONDS)


def remaining_seconds(deadline_epoch: float, *, now: float | None = None) -> float:
    return float(deadline_epoch) - (time.time() if now is None else now)


def is_exhausted(deadline_epoch: float, *, now: float | None = None) -> bool:
    return remaining_seconds(deadline_epoch, now=now) <= 0.0


def cap_child_budget(child_seconds: float, deadline_epoch: float | None,
                     *, now: float | None = None) -> float:
    child = float(child_seconds)
    if not deadline_epoch:
        return child
    rem = remaining_seconds(deadline_epoch, now=now)
    if rem <= 0:
        return 0.0
    return min(child, rem)


def cap_child_deadline_epoch(child_deadline_epoch: float, pipeline_deadline_epoch: float | None) -> float:
    if not pipeline_deadline_epoch:
        return float(child_deadline_epoch)
    return min(float(child_deadline_epoch), float(pipeline_deadline_epoch))


__all__ = ["deadline_epoch_from_created_at", "remaining_seconds", "is_exhausted",
           "cap_child_budget", "cap_child_deadline_epoch"]


# #
# THE START DECISION - may this run begin at all?
# Extracted from application/pipeline_run._run_async. The budget POLICY lives here; the durable
# refusal that follows an exhausted verdict lives in terminal_finalize, because persisting a
# terminal state is that authority's role and not this module's. `_run_async` routes between them.
# #

#: What the user is told when the whole logical budget was already spent before the run started.
#: Blameless and free of internals - the cause is our scheduling, not their geometry.
EXHAUSTED_MESSAGE = "The job ran out of its total time budget before it could run."


class StartDecision(NamedTuple):

    exhausted: bool
    deadline_epoch: float
    reason: str = ""

    @property
    def may_start(self) -> bool:
        return not self.exhausted


def anchor_deadline(ownership, job_created_at) -> float:
    if ownership is not None and getattr(ownership, "pipeline_deadline_at", None) is not None:
        return ownership.pipeline_deadline_at.timestamp()
    return deadline_epoch_from_created_at(job_created_at)


def decide_start(ownership, job_created_at, *, now: float | None = None) -> StartDecision:
    deadline = anchor_deadline(ownership, job_created_at)
    if is_exhausted(deadline, now=now):
        return StartDecision(True, deadline, "pipeline_timed_out")
    return StartDecision(False, deadline)
