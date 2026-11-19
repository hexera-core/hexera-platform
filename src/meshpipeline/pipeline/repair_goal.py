# Responsibility: Turn a CUSTOMER-SUPPLIED repair goal request into the two values the repair loop
#                 takes - the goal profile and the auto-accept deviation threshold - or refuse it.
# Owns: the accepted goal names, the bound on a declared threshold, and the posture that makes a
#       standalone repair job reachable at all.
# Boundaries: PURE. It parses, validates and answers; it repairs nothing, dispatches nothing and
#             reads no database. The threshold it returns is the only one the session will see, so
#             this is the single place an untrusted number is judged.
# Collaborates with: cad/repair/contracts.py (`RepairGoal`), cad/repair/review_tier.py (which
#                    vetoes a vertex-moving package at ANY threshold this module would allow),
#                    application/dispatch_contract.py and api/v1/admin_repair.py (the producers),
#                    pipeline/repair_attempt.py and pipeline/repair_deliver.py (the readers).
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.contracts import RepairGoal

logger = logging.getLogger(__name__)

#: The goals a REQUEST may name. `benchmark` is the sealed track's own (devtools), never a
#: customer's: a job surface that accepted it would let a caller ask for the evaluation profile on
#: a production part.
REQUESTABLE = (RepairGoal.standalone, RepairGoal.meshing)

#: Millimetres. The widest auto-accept threshold any request may declare. A threshold is the
#: deviation at which an unreviewed commit becomes acceptable, so an unbounded one is a request to
#: skip the reviewer entirely - which is exactly what an untrusted field must not be able to buy.
#: One millimetre is already larger than the repair's own default tolerance; a part that needs more
#: than this moved is a person's decision, not a number's.
MAX_AUTO_ACCEPT_MM = 1.0

#: The key the request carries the threshold under, as the API body and the stored payload spell it.
THRESHOLD_FIELD = "auto_accept_deviation_mm"


class GoalRequestError(ValueError):
    """A repair goal request that may not be run. Raised at the PRODUCER's boundary only."""


@dataclass(frozen=True)
class GoalRequest:
    """What a run was asked to finish for, and the only threshold it may widen anything with."""

    goal: RepairGoal
    auto_accept_mm: float | None

    def to_state(self) -> dict:
        """The dict form that travels payload -> request -> state. JSON-safe, and re-validated on
        the way out by `from_state`: the state is rebuilt from a stored payload, so the round trip
        is a trust boundary in both directions."""
        return {"goal": self.goal.value, THRESHOLD_FIELD: self.auto_accept_mm}


def posture() -> str:
    """Whether standalone repair jobs are reachable, FAILING CLOSED: only the exact `live`.

    Read with `getattr` and defaulted, so a deployment whose settings module predates the row
    (`cad-repair-standalone-jobs`) behaves exactly as it does today rather than failing to import.
    """
    return "live" if getattr(rcfg, "CAD_REPAIR_STANDALONE_JOBS", "off") == "live" else "off"


def _threshold(value: object) -> float | None:
    """A declared deviation threshold, validated. UNTRUSTED INPUT: this is the whole guard.

    `None` is "none declared", which leaves the auto-accept tier inert. Anything else must be a
    real number - a `bool` is not one, and neither is a numeric string, because accepting either
    means the bound was checked against something the session will later read differently - and it
    must be finite, non-negative and at or under `MAX_AUTO_ACCEPT_MM`.

    `math.isfinite` IS NOT OPTIONAL. `-inf` and `nan` sail through a naive `<= bound` comparison
    (`nan <= x` is False in both directions, so a one-sided guard reads it as whichever side it
    tested), and a merged lane found exactly that.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GoalRequestError(
            f"{THRESHOLD_FIELD} must be a number of millimetres or omitted, not "
            f"{type(value).__name__}")
    number = float(value)
    if not math.isfinite(number):
        raise GoalRequestError(f"{THRESHOLD_FIELD} must be a finite number, not {value!r}")
    if number < 0.0:
        raise GoalRequestError(f"{THRESHOLD_FIELD} may not be negative ({number!r})")
    if number > MAX_AUTO_ACCEPT_MM:
        raise GoalRequestError(
            f"{THRESHOLD_FIELD} {number!r} is over the {MAX_AUTO_ACCEPT_MM} mm bound - a wider "
            "threshold is a request to skip the reviewer, which is a person's decision")
    return number


def parse(payload: object) -> GoalRequest:
    """The request a producer was handed, validated, or `GoalRequestError`.

    Raised rather than swallowed BECAUSE THE CALLER IS AN API: a customer who asked for something
    this service will not do must be told, at the boundary, in the same request. Inside a node the
    answer is `from_state`, which never raises.
    """
    if not isinstance(payload, dict):
        raise GoalRequestError(
            f"a repair goal request must be an object with a `goal`, not {type(payload).__name__}")
    name = payload.get("goal")
    goal = next((g for g in REQUESTABLE if name == g.value), None)
    if goal is None:
        raise GoalRequestError(
            f"`goal` must be one of {[g.value for g in REQUESTABLE]}, not {name!r}")
    return GoalRequest(goal=goal, auto_accept_mm=_threshold(payload.get(THRESHOLD_FIELD)))


def from_state(state) -> GoalRequest | None:
    """The goal this run declared, or `None` - which is TODAY'S RUN, byte for byte.

    NEVER RAISES, and fails closed in three ways: the posture must be exactly live, the stored
    request must still validate (it was written by a producer, but it was stored, and a stored
    value is read again rather than trusted again), and anything unreadable is no goal at all.
    """
    if posture() != "live":
        return None
    try:
        raw = state.get("repair_goal") if hasattr(state, "get") else None
    except Exception:  # noqa: BLE001 - an unreadable state is no goal
        return None
    if not raw:
        return None
    try:
        return parse(raw)
    except GoalRequestError as exc:
        # A STORED REQUEST THAT NO LONGER VALIDATES IS DROPPED, NOT RUN. The bound may have been
        # lowered since it was written, and a run is never widened by the age of its payload.
        logger.warning("repair_goal: a stored goal request was refused (%s) - the run keeps "
                       "today's behaviour", exc)
        return None
