# Responsibility: Record how long real meshes took, and estimate from that history.
# Boundaries: recording and estimation only; it enforces no timeout.
# Collaborates with: contracts/mesh_timing.py.
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Enough to be a distribution, few enough that the estimate tracks a changing engine
# rather than being dragged by runs from a year ago.
_MAX_SAMPLES = 200

# Below this, a "median" is an anecdote. We show the declared budget instead and say so.
MIN_SAMPLES_FOR_ESTIMATE = 3

# Nothing real meshes an actual geometry in under a second. A sub-second rc==0 is a
# no-op, a cached result, or an instant path - not a timing sample, and it rounds to
# "0.0" under the .1f store anyway. Reject it on the way in AND on the way out (old
# garbage may already be in the store).
_MIN_PLAUSIBLE_SECONDS = 1.0


def record(engine: str, purpose: str, seconds: float) -> None:
    if seconds < _MIN_PLAUSIBLE_SECONDS:
        return
    try:
        from meshpipeline.contracts.mesh_timing import append_sample
        append_sample(engine, purpose, seconds, _MAX_SAMPLES)
    except Exception as exc:  # noqa: BLE001
        logger.debug("mesh_history: could not record %s/%s - %s", engine, purpose, exc)


def estimate(engine: str, purpose: str) -> dict | None:
    try:
        from meshpipeline.contracts.mesh_timing import read_samples
        raw = read_samples(engine, purpose)
    except Exception as exc:  # noqa: BLE001
        logger.debug("mesh_history: could not read %s/%s - %s", engine, purpose, exc)
        return None

    vals = sorted(v for v in raw if v >= _MIN_PLAUSIBLE_SECONDS)
    if len(vals) < MIN_SAMPLES_FOR_ESTIMATE:
        return None
    return {
        "n":         len(vals),
        "typical_s": int(_pct(vals, 0.50)),
        "p10_s":     int(_pct(vals, 0.10)),
        "p90_s":     int(_pct(vals, 0.90)),
    }


def _pct(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    i = min(len(sorted_vals) - 1, max(0, round(q * (len(sorted_vals) - 1))))
    return sorted_vals[int(i)]
