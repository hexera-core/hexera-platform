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

    @classmethod
    def of(cls, limits: LoopLimits, deadline_s: float | None) -> LoopBudget:
        return cls(deadline=None if deadline_s is None else time.monotonic() + float(deadline_s),
                   max_rounds=limits.max_rounds)

    def remaining(self) -> float | None:
        return None if self.deadline is None else self.deadline - time.monotonic()

    def expired(self) -> bool:
        rem = self.remaining()
        return rem is not None and rem <= 0

    def may_start_round(self, rounds_done: int) -> bool:
        return self.max_rounds is None or rounds_done < self.max_rounds


__all__ = ["LoopBudget"]
