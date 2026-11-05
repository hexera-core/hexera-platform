# Responsibility: Own the run's one wall-clock ceiling and hand out what remains of it.
# Owns: the deadline anchored once at execution start, remaining time, and the cap on every child budget.
# Boundaries: the deadline is never reset, so a retry cannot extend a run by restarting its clock.
# Only PROGRESS moves it on (progress_extension): a run still making progress at its deadline is
# never ended by the clock, and one that has stopped is ended as before.
from __future__ import annotations

import time
from datetime import datetime
from typing import NamedTuple

import meshpipeline.settings.runtime as rtcfg

#: PROGRESS KEEPS A RUN GOING PAST ITS DEADLINE. A run that showed progress (a mesher's new output,
#: a new step: contracts/progress_clock) less than this long ago is not out of time: its deadline is
#: moved to this long after that progress. It is the FEA stall window (engines/gmsh/gmsh_runner.
#: FEA_STALL_WINDOW_S, 900 s - the quiet after which a mesher is stopped as stuck) plus two
#: heartbeats of slack, so the deadline never ends a run the stall watch would have let go on.
#: (dev-areen 25 Oct: a pass still meshing was cut at "the hard limit of 110 minutes" and its retry
#: got 1 minute - the clock, not the mesh, ended the job.)
PROGRESS_GRACE_S = 900 + 120

#: Deadlines moved on by progress in THIS process, by the deadline the run was anchored with. The
#: run's state carries the anchored value (seeded once, checkpointed), and every reader asks
#: through effective_deadline, so one extension reaches all of them. A worker runs one job at a time.
_EXTENDED: dict[float, float] = {}


def effective_deadline(deadline_epoch: float) -> float:
    """The run's deadline as progress has moved it (never earlier than the anchored one)."""
    d = float(deadline_epoch)
    return max(d, _EXTENDED.get(d, d))


def extend(deadline_epoch: float, to_epoch: float) -> float:
    """Move the run anchored at `deadline_epoch` on to `to_epoch` (never back). Returns the deadline now."""
    d = float(deadline_epoch)
    _EXTENDED[d] = max(effective_deadline(d), float(to_epoch))
    return _EXTENDED[d]


def progress_extension(*, now: float | None = None) -> float | None:
    """Where progress would move the deadline to: PROGRESS_GRACE_S after the last sign of progress
    this process saw; None when it saw none that recent (the deadline stands)."""
    from meshpipeline.contracts import progress_clock
    age = progress_clock.seconds_since()
    if age is None or age > PROGRESS_GRACE_S:
        return None
    return (time.time() if now is None else now) - age + PROGRESS_GRACE_S


def deadline_epoch_from_created_at(created_at: datetime | None) -> float:
    base = created_at.timestamp() if created_at is not None else time.time()
    return base + float(rtcfg.PIPELINE_TOTAL_TIMEOUT_SECONDS)


def remaining_seconds(deadline_epoch: float, *, now: float | None = None) -> float:
    return effective_deadline(deadline_epoch) - (time.time() if now is None else now)


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
    return min(float(child_deadline_epoch), effective_deadline(pipeline_deadline_epoch))


__all__ = ["PROGRESS_GRACE_S", "deadline_epoch_from_created_at", "effective_deadline", "extend",
           "progress_extension", "remaining_seconds", "is_exhausted", "cap_child_budget",
           "cap_child_deadline_epoch"]


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
