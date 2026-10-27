# Responsibility: Settle how many provider rounds this attempt may take.
# Boundaries: a budget derived from configuration and the run's remaining time; the loop enforces it.
from __future__ import annotations

import time
from dataclasses import dataclass

import meshpipeline.agents.builder.settings as bcfg


@dataclass(frozen=True)
class AttemptBudget:

    deadline_epoch: float
    remaining_s: int
    #: int, not float: `_run_tool_loop` takes an integer budget, and `min()` over two ints is the
    #: same value the node computed before this module existed.
    attempt_timeout_s: int
    #: what a DETERMINISTIC ENGINE DRIVER may use (the FEA driver, PLAN-fea-13: no guessed time limit):
    #: the pipeline's remaining time - its one absolute ceiling - not the model loop's aggregate budget,
    #: which is sized for model rounds (BUILDER_TOTAL_TIMEOUT_SECONDS) and stopped a 1.1M-element mesher
    #: that was still making progress (fw_case, dev-areen 24 Oct). Without a pipeline deadline it is
    #: remaining_s, as before.
    driver_remaining_s: int = 0

    @property
    def exhausted(self) -> bool:
        return self.remaining_s <= 0

    @property
    def driver_exhausted(self) -> bool:
        """A DETERMINISTIC DRIVER's attempt is out of time only when the pipeline is - never because
        the model loop's aggregate budget was spent by an earlier attempt's long, progressing mesh
        (an FEA retry after a 2.5-hour first pass was refused as "exceeded its overall time budget")."""
        return self.driver_remaining_s <= 0


def settle(*, carried_epoch: float, pipeline_deadline_epoch, now: float | None = None,
           native_s: float = 0.0) -> AttemptBudget:
    from meshpipeline.application.pipeline_budget import cap_child_deadline_epoch, remaining_seconds

    clock = time.time() if now is None else now
    epoch = float(carried_epoch or 0.0)
    if epoch <= 0.0:
        epoch = clock + bcfg.BUILDER_TOTAL_TIMEOUT_SECONDS
    epoch = cap_child_deadline_epoch(epoch, pipeline_deadline_epoch)
    # THE AGGREGATE BUDGET IS MODEL TIME: the seconds this run spent inside native mesh runs
    # (`native_s`, contracts/progress_clock.native_seconds) are given back - a long, progressing
    # mesh never spends the builder's budget for model rounds. The epoch itself is kept as it was
    # set, so a later attempt adds the native time once, not twice.
    remaining = int(epoch + max(0.0, float(native_s)) - clock)
    # the pipeline's time as progress has moved it (pipeline_budget.effective_deadline)
    pipeline_left = int(remaining_seconds(pipeline_deadline_epoch, now=clock)) if pipeline_deadline_epoch else 0
    return AttemptBudget(deadline_epoch=epoch, remaining_s=remaining,
                         attempt_timeout_s=min(bcfg.BUILDER_LOOP_TIMEOUT, remaining),
                         driver_remaining_s=max(remaining, pipeline_left))


__all__ = ["AttemptBudget", "settle"]
