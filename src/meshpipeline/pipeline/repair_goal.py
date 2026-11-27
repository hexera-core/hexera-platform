# Responsibility: Turn a CUSTOMER-SUPPLIED repair goal request into what the run takes - the
#                 three-way choice (Repair + mesh | Repair CAD only | Mesh only), the auto-accept
#                 deviation threshold and the per-run review bypass - or refuse it.
# Owns: the accepted choice words, the bound on a declared threshold, the bypass bit's shape, and
#       the posture that gates the OPERATOR's standalone re-run route.
# Boundaries: PURE. It parses, validates and answers; it repairs nothing, dispatches nothing and
#             reads no database. The threshold it returns is the only one the session will see, so
#             this is the single place an untrusted number is judged. Whether an organisation MAY
#             bypass review is the producers' question (application/repair_settings.py); this
#             module only carries the bit a producer already allowed.
# Collaborates with: cad/repair/contracts.py (`RepairGoal`), cad/repair/review_tier.py (which
#                    vetoes a vertex-moving package at ANY threshold this module would allow),
#                    application/dispatch_contract.py, agents/intake/approval.py and
#                    api/v1/admin_repair.py (the producers), pipeline/graph.py,
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

#: THE CUSTOMER'S THREE-WAY CHOICE (CRS v5 beta, Decision 1), as the dispatch payload spells it.
#: `meshing` is "Repair + mesh" (today's run, the default), `standalone` is "Repair CAD only" (the
#: run ends with the repaired STEP), `off` is "Mesh only" (no repair check and no repair at all).
CHOICE_MESHING = "meshing"
CHOICE_STANDALONE = "standalone"
CHOICE_OFF = "off"
CHOICES = (CHOICE_MESHING, CHOICE_STANDALONE, CHOICE_OFF)
DEFAULT_CHOICE = CHOICE_MESHING

#: The key a request carries the per-run review bypass under ("auto-accept repairs, skip review").
#: The ORGANISATION's permission is checked by the producers (the geometry-check confirm and the
#: approval that builds the dispatch payload); a payload carries the bit only once it was allowed.
BYPASS_FIELD = "auto_accept_repairs"


class GoalRequestError(ValueError):
    """A repair goal request that may not be run. Raised at the PRODUCER's boundary only."""


@dataclass(frozen=True)
class GoalRequest:
    """What a run was asked to finish for, and the only threshold it may widen anything with."""

    goal: RepairGoal
    auto_accept_mm: float | None
    #: "Mesh only": the run skips the repair check and every repair (inspect, attempt, handback).
    #: `goal` stays `meshing` so every reader typed on `RepairGoal` reads a meshing run.
    repair_off: bool = False
    #: The per-run review bypass: repairs that would stop for a person's review are applied
    #: without it, and the run records that they were auto-accepted. Default False: review.
    bypass_review: bool = False

    @property
    def choice(self) -> str:
        """The customer's word for this request: meshing | standalone | off."""
        if self.repair_off:
            return CHOICE_OFF
        return CHOICE_STANDALONE if self.goal is RepairGoal.standalone else CHOICE_MESHING

    def to_state(self) -> dict:
        """The dict form that travels payload -> request -> state. JSON-safe, and re-validated on
        the way out by `from_state`: the state is rebuilt from a stored payload, so the round trip
        is a trust boundary in both directions."""
        out: dict = {"goal": self.choice, THRESHOLD_FIELD: self.auto_accept_mm}
        if self.bypass_review:
            out[BYPASS_FIELD] = True
        return out


def posture() -> str:
    """Whether the OPERATOR's standalone re-run route is reachable, FAILING CLOSED: only `live`.

    Read with `getattr` and defaulted, so a deployment whose settings module predates the row
    (`cad-repair-standalone-jobs`) behaves exactly as it does today rather than failing to import.
    The customer's own three-way choice is not behind it (see `from_state`).
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


def _bypass(value: object) -> bool:
    """The per-run bypass bit, validated: a real boolean or absent. A string "false" is not False."""
    if value is None:
        return False
    if not isinstance(value, bool):
        raise GoalRequestError(f"{BYPASS_FIELD} must be true or false, not {type(value).__name__}")
    return value


def parse(payload: object) -> GoalRequest:
    """The request a producer was handed, validated, or `GoalRequestError`.

    Accepts the bare choice word (`"meshing"` | `"standalone"` | `"off"`) or the object form
    `{"goal": <choice>, "auto_accept_deviation_mm": float|None, "auto_accept_repairs": bool}`.

    Raised rather than swallowed BECAUSE THE CALLER IS AN API: a customer who asked for something
    this service will not do must be told, at the boundary, in the same request. Inside a node the
    answer is `from_state`, which never raises.
    """
    if isinstance(payload, str):
        payload = {"goal": payload}
    if not isinstance(payload, dict):
        raise GoalRequestError(
            f"a repair goal request must be an object with a `goal`, not {type(payload).__name__}")
    name = payload.get("goal")
    bypass = _bypass(payload.get(BYPASS_FIELD))
    threshold = _threshold(payload.get(THRESHOLD_FIELD))
    if name == CHOICE_OFF:
        # MESH ONLY: no repair runs, so there is nothing to accept or bypass - a bypass or a
        # threshold beside it is dropped rather than recorded as a licence nothing used
        return GoalRequest(goal=RepairGoal.meshing, auto_accept_mm=None, repair_off=True)
    goal = next((g for g in REQUESTABLE if name == g.value), None)
    if goal is None:
        raise GoalRequestError(f"`goal` must be one of {list(CHOICES)}, not {name!r}")
    return GoalRequest(goal=goal, auto_accept_mm=threshold, bypass_review=bypass)


def from_state(state) -> GoalRequest | None:
    """The goal this run declared, or `None` - which is TODAY'S RUN ("Repair + mesh"), byte for byte.

    NEVER RAISES, and fails closed in two ways: the stored request must still validate (it was
    written by a producer, but it was stored, and a stored value is read again rather than trusted
    again), and anything unreadable is no goal at all.

    THE CUSTOMER'S CHOICE IS NOT BEHIND THE POSTURE (CRS v5 beta, Decision 1): the three-way
    choice is a product feature every customer has, so a goal a producer validated and stored is
    honoured on every deployment. `posture()` still gates the OPERATOR's admin route
    (api/v1/admin_repair.py), which re-runs an existing job as a standalone repair.
    """
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


def choice_of(state) -> str:
    """The customer's three-way choice for this run: meshing (the default) | standalone | off."""
    asked = from_state(state)
    return DEFAULT_CHOICE if asked is None else asked.choice


def repair_off(state) -> bool:
    """True when the customer chose "Mesh only": no repair check and no repair of any kind."""
    asked = from_state(state)
    return asked is not None and asked.repair_off


def bypass_review(state) -> bool:
    """True when the run was dispatched with the review bypass ("auto-accept repairs")."""
    asked = from_state(state)
    return asked is not None and asked.bypass_review
