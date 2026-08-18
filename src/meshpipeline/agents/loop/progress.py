# Responsibility: Track whether a loop is still making progress.
# Boundaries: observation; the response to a stall belongs to the role's policy.
from __future__ import annotations

from meshpipeline.contracts.agent_loop import LoopLimits, ProgressObservation


def advance(observation: ProgressObservation, *, progress_count: int,
            consecutive_no_progress: int, last_signature: str | None,
            ) -> tuple[int, int, str | None]:
    if observation.made_progress:
        return progress_count + 1, 0, observation.signature or None
    if observation.signature and observation.signature == last_signature:
        return progress_count, consecutive_no_progress + 1, last_signature
    return progress_count, 1, (observation.signature or None)


def stalled(consecutive_no_progress: int, limits: LoopLimits) -> bool:
    if limits.no_progress_threshold is None:
        return False
    return consecutive_no_progress >= limits.no_progress_threshold


__all__ = ["advance", "stalled"]
