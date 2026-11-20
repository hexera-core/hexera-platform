# Responsibility: Decide WHICH REVIEWER a package that passed its predicate gets - the model, or
#                 the audited auto-accept tier - on measured evidence alone.
# Owns: VERTEX_MOVING_OPS (the documented set of operations licensed to move vertices) and the
#       tier decision itself. Nothing else in the repair decides this.
# Boundaries: PURE and it NEVER RAISES. It reads numbers and sets and answers; it applies nothing,
#             measures nothing, commits nothing and calls no model. An internal error is answered
#             `reviewer` - the fail-closed direction, because the cost of a wrong `auto` is an
#             unreviewed commit on a customer's part and the cost of a wrong `reviewer` is one
#             model call.
# Collaborates with: cad/repair/session.py (`try_package`, its only caller),
#                    cad/repair/package_review.py (the deviation statuses and the ACCEPT verdict
#                    this tier stands in for), settings/cad_repair.py (the posture).
from __future__ import annotations

import logging
import math
from collections.abc import Iterable
from typing import Any

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair import caps, package_review

logger = logging.getLogger(__name__)

#: The tier names. `auto` is an ACCEPT RECORDED WITHOUT A MODEL CALL, never a skipped review:
#: the verdict still carries the reason and the clipped evidence it was made on.
AUTO = "auto"
REVIEWER = "reviewer"

#: THE OPERATIONS LICENSED TO MOVE VERTICES (settings/cad_repair.py: CAD_REPAIR_REBUILD_FACE and
#: CAD_REPAIR_CLOSE_WIRE_GAPS, both live only on the exact `crs_v3=live`, which also makes the
#: reviewer live). A vertex move changes where the customer's surface IS, not merely how it is
#: described, so it is never auto-accepted at any deviation and under any threshold. The deploy
#: expressions already make these ride with the reviewer; this set is the same rule stated where
#: the tier decision is made, so a deploy change cannot quietly hand them to the auto tier.
VERTEX_MOVING_OPS = frozenset({"close_wire_gaps", "rebuild_face"})


def posture() -> str:
    """The auto-accept posture, FAILING CLOSED: only the exact string `live` turns it on."""
    return "live" if getattr(rcfg, "CAD_REPAIR_AUTO_ACCEPT", "off") == "live" else "off"


def declaration_covers(*, before, after, declared_area_mm2: float,
                       declared_volume_mm3: float) -> bool:
    """Do the kept operations' DECLARATIONS account for what was actually measured?

    THE SAME COMPARISON `caps` ALREADY MAKES, called rather than re-derived: the residual ratios
    and the arithmetic are the caps' and must not exist twice. `caps._accounted` returns a REASON
    when the change is unaccounted for and an empty string when it is covered, so an empty answer
    on both area and volume is a declaration that covers its measurement.

    An unmeasurable measurement (a non-finite area or volume) is NOT covered: the auto tier may
    only ever answer on numbers it actually has.
    """
    try:
        for original, current, declared, residual in (
                (before.surface_area_mm2, after.surface_area_mm2, declared_area_mm2,
                 rcfg.CAD_REPAIR_MAX_AREA_CHANGE_RATIO),
                (before.volume_mm3, after.volume_mm3, declared_volume_mm3,
                 rcfg.CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO)):
            if not (math.isfinite(float(original)) and math.isfinite(float(current))
                    and math.isfinite(float(declared))):
                # NOT COVERED. `caps._accounted` divides, and a NaN unexplained ratio compares
                # False against every residual - so a measurement that did not measure would read
                # as perfectly accounted for. The auto tier answers on numbers it actually has.
                return False
            if caps._accounted(float(original), float(current), float(declared), float(residual)):
                return False
        return True
    except Exception as exc:  # noqa: BLE001 - an unreadable measurement is NOT covered
        logger.info("review_tier: declaration coverage unreadable (%s)", type(exc).__name__)
        return False


def _kept_op_names(verdicts: Iterable[Any]) -> list[str]:
    return [str(getattr(v, "op", "")) for v in verdicts if bool(getattr(v, "kept", False))]


#: The deviation bases a threshold may be judged on: the faces the operations named, or every face
#: the upload lacks. The REMOVAL FALLBACKS (`deviation.BASIS_ADJACENT`, `deviation.BASIS_WHOLE`)
#: sample surviving surface and never the surface that went, so a number on them says nothing
#: about what was removed and may not buy an auto-accept. An absent basis is a session that
#: predates the field, which only ever measured edited or changed faces.
_TIER_BASES = frozenset({"", "edited_faces", "changed_faces"})


def _kept_removals(verdicts: Iterable[Any]) -> list[str]:
    return [str(getattr(v, "op", "")) for v in verdicts if bool(getattr(v, "kept", False))
            and package_review.removes_surface(getattr(v, "evidence", None) or {})]


def decide(*, verdicts: Iterable[Any], reclassified: frozenset, revealed: frozenset,
           declared_matches: bool, deviation: dict, deviation_status: str,
           threshold_mm: float | None) -> tuple[str, str]:
    """`("auto", reason)` or `("reviewer", reason)` for one package that passed its predicate.

    EVERY CONDITION IS A VETO and they are checked in a fixed order, so the reason a reader gets
    is the FIRST thing that sent the package to a person. `auto` needs ALL of:

    - the posture is live;
    - the deployment carries a customer threshold above zero;
    - no kept operation moves vertices (`VERTEX_MOVING_OPS`);
    - no kept operation REMOVED surface (`package_review.removes_surface`): the deviation samples
      the result, never the surface that went, so no number can show a removal stayed in bounds;
    - nothing was discharged by relabelling (`reclassified`) and nothing new appeared
      (`revealed`) - either is a judgement call, which is what the reviewer's seat is for;
    - the kept operations' declarations cover what was measured (`declared_matches`);
    - the deviation was MEASURED - not off, not out of time, not failed - and is at or under the
      threshold.

    - and the deviation's basis is one a threshold can be judged on (`_TIER_BASES`).

    NEVER RAISES. Anything unexpected is `reviewer`.
    """
    try:
        verdicts = list(verdicts)
        if posture() != "live":
            return REVIEWER, "the auto-accept posture is off"
        if threshold_mm is None:
            return REVIEWER, "no customer deviation threshold was declared"
        threshold = float(threshold_mm)
        if not threshold > 0.0:  # NaN-safe
            return REVIEWER, f"the declared threshold {threshold_mm!r} is not above zero"
        moving = sorted(set(_kept_op_names(verdicts)) & VERTEX_MOVING_OPS)
        if moving:
            return REVIEWER, f"the package moves vertices ({', '.join(moving)})"
        removing = sorted(set(_kept_removals(verdicts)))
        if removing:
            return REVIEWER, (f"the package removed surface ({', '.join(removing)}); a removed "
                              "surface is never sampled, so no deviation can clear it")
        if reclassified:
            return REVIEWER, (f"{len(reclassified)} defect(s) were discharged by relabelling "
                              "rather than by repair")
        if revealed:
            return REVIEWER, f"{len(revealed)} new finding(s) appeared under the repair"
        if not declared_matches:
            return REVIEWER, "the measured change is not covered by what the operations declared"
        if str(deviation_status) != package_review.DEVIATION_MEASURED:
            return REVIEWER, f"the deviation was not measured ({deviation_status or 'absent'})"
        basis = str(deviation.get("deviation_basis") or "")
        if basis not in _TIER_BASES:
            return REVIEWER, (f"the deviation was sampled on {basis}, which never measures the "
                              "surface a removal took away")
        measured = deviation.get("deviation_mm")
        if measured is None:
            return REVIEWER, "the deviation reads measured but carries no value"
        measured_mm = float(measured)
        if not math.isfinite(measured_mm):
            # A NON-FINITE DEVIATION IS NOT A MEASUREMENT. An infinity compares harmlessly small
            # in one direction and is no answer in either, so it goes to a person.
            return REVIEWER, f"the deviation did not measure to a number ({measured!r})"
        if not measured_mm <= threshold:  # NaN-safe: a deviation over the threshold is a person's
            return REVIEWER, (f"the deviation {measured_mm:.6g} mm is over the "
                              f"{threshold:.6g} mm threshold")
        return AUTO, (f"a fully accounted change {measured_mm:.6g} mm from the upload, within "
                      f"the {threshold:.6g} mm threshold")
    except Exception as exc:  # noqa: BLE001 - the tier decision never raises; it fails closed
        logger.info("review_tier: decision unreadable (%s)", type(exc).__name__)
        return REVIEWER, f"the tier decision could not be made ({type(exc).__name__})"
