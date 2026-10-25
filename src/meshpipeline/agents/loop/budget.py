# Responsibility: Hold a loop's round budget and report when it is spent.
# Boundaries: counting rounds, not tool calls - an agent may issue several calls inside one round.
from __future__ import annotations

import time
from dataclasses import dataclass

from meshpipeline.contracts.agent_loop import LoopLimits


@dataclass(frozen=True)
class LoopBudget:

    deadline: float | None = None
    max_rounds: int | None = None

    # MODEL TIME (contracts/progress_clock.model_time): the clock stands still while a native
    # mesher runs, so a loop's budget is spent on its rounds, never on a long mesh it is waiting for
    @classmethod
    def of(cls, limits: LoopLimits, deadline_s: float | None) -> LoopBudget:
        return cls(deadline=None if deadline_s is None else _now() + float(deadline_s),
                   max_rounds=limits.max_rounds)

    def remaining(self) -> float | None:
        return None if self.deadline is None else self.deadline - _now()

    def expired(self) -> bool:
        rem = self.remaining()
        return rem is not None and rem <= 0

    def may_start_round(self, rounds_done: int) -> bool:
        return self.max_rounds is None or rounds_done < self.max_rounds


def _now() -> float:
    # time.monotonic, less the time spent inside native mesh runs (progress_clock.model_time)
    from meshpipeline.contracts.progress_clock import native_seconds
    return time.monotonic() - native_seconds()


__all__ = ["LoopBudget"]
