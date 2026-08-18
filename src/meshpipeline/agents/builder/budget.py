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

    @property
    def exhausted(self) -> bool:
        return self.remaining_s <= 0


def settle(*, carried_epoch: float, pipeline_deadline_epoch, now: float | None = None
           ) -> AttemptBudget:
    from meshpipeline.application.pipeline_budget import cap_child_deadline_epoch

    clock = time.time() if now is None else now
    epoch = float(carried_epoch or 0.0)
    if epoch <= 0.0:
        epoch = clock + bcfg.BUILDER_TOTAL_TIMEOUT_SECONDS
    epoch = cap_child_deadline_epoch(epoch, pipeline_deadline_epoch)
    remaining = int(epoch - clock)
    return AttemptBudget(deadline_epoch=epoch, remaining_s=remaining,
                         attempt_timeout_s=min(bcfg.BUILDER_LOOP_TIMEOUT, remaining))


__all__ = ["AttemptBudget", "settle"]
