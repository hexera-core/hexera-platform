# Responsibility: Hold a loop's round budget and report when it is spent.
# Boundaries: counting rounds, not tool calls - an agent may issue several calls inside one round.
from __future__ import annotations

import asyncio
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


async def wait_on_model_time(work, *, timeout: float | None, poll_s: float = 5.0):
    """asyncio.wait_for, with the clock standing still while a native mesher runs
    (contracts/progress_clock.model_time): `work` is stopped at `timeout` seconds of MODEL time,
    never in the middle of a mesh that is still making progress (that run has its own stall watch
    and machine wall, contracts/native_time). No timeout: simply awaited. Raises TimeoutError as
    wait_for does."""
    from meshpipeline.contracts.progress_clock import model_time, native_running
    task = asyncio.ensure_future(work)
    if timeout is None:
        return await task
    deadline = model_time() + float(timeout)
    try:
        while True:
            left = deadline - model_time()
            if left <= 0 and not native_running():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                raise TimeoutError()
            done, _ = await asyncio.wait({task}, timeout=max(0.05, min(poll_s, left) if left > 0 else poll_s))
            if done:
                return task.result()
    except asyncio.CancelledError:
        task.cancel()
        raise


__all__ = ["LoopBudget", "wait_on_model_time"]
