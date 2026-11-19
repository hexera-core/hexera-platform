# Responsibility: END a STANDALONE repair job - say what the customer is getting back, name the
#                 finishing verdict the repair already produced, and say so plainly when the part
#                 could not be repaired.
# Owns: the `repair_delivery` record a standalone run ends with. Nothing else writes it.
# Boundaries: it DELIVERS NOTHING ITSELF. The bytes and the report are handed back by the
#             orchestrator's terminal path (application/repair_report_delivery.py), which is the
#             only place that holds this worker's fence - a node that uploaded behind the fence
#             could leave rows on a job it no longer speaks for. This node states the ending; the
#             terminal path performs it. It never raises: a standalone run must end.
# Collaborates with: pipeline/repair_attempt.py (which repaired and recorded the verdict),
#                    pipeline/repair_goal.py (the goal that routed the run here),
#                    application/pipeline_run.py (the terminal path that delivers what this names).
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: `repair_delivery["outcome"]`. A closed word, read by the ending's words rather than its prose.
REPAIRED = "repaired"
UNREPAIRABLE = "unrepairable"

#: What an ending says when the run left no explanation of its own. Never empty: a customer told
#: only "unrepairable" has been given a verdict without a reason.
_NO_REASON = "the repair left no usable result and recorded no reason"


def _record(attempt) -> dict:
    """The attempt record as a dict, or {} for anything that is not one."""
    return dict(attempt) if isinstance(attempt, dict) else {}


def _deliverable(attempt: dict) -> tuple[str, str]:
    """`(path, suffix)` of the repaired file this run can actually hand back, or `("", "")`.

    CHECKED ON DISK, not inferred from the record. A resumed or taken-over execution re-derives
    the repair into a workspace that may no longer exist, and naming bytes the delivery cannot
    read would turn an honest "we could not repair it" into a download that 404s.
    """
    named = attempt.get("repaired_path")
    if not isinstance(named, str) or not named:
        return "", ""
    try:
        path = Path(named)
        if not path.is_file():
            return "", ""
        return str(path), (path.suffix.lower() or ".step")
    except Exception as exc:  # noqa: BLE001 - an unreadable path is no deliverable
        logger.warning("node_repair_deliver: the repaired file could not be read (%s)",
                       type(exc).__name__)
        return "", ""


def _why(attempt: dict) -> str:
    """Why this part is not coming back repaired, in the run's OWN words.

    The reasons are the attempt record's, in the order a reader needs them: an attempt that never
    happened explains itself (`reason`), and one that happened but did not resolve is explained by
    where the loop stopped. Honest endings are first-class - an unrepairable part must come back
    saying so, not come back silent.
    """
    for key in ("reason", "stopped_because", "summary"):
        said = attempt.get(key)
        if isinstance(said, str) and said.strip():
            return said.strip()
    return _NO_REASON


async def node_repair_deliver(state) -> dict:
    """The standalone repair job's ending. NEVER RAISES - a job that cannot be ended is worse
    than one that ends badly, and this node is the last thing on that path.

    It REPORTS the finishing verdict `cad/repair/goal.finishing_verdict` already made (kernel
    clean, the tier-2 meshability score, the grind tier and the committed packages' review
    record); it does not recompute one, and it never reaches the product mesher.
    """
    try:
        attempt = _record(state.get("repair_attempt") if hasattr(state, "get") else None)
        measurements = attempt.get("measurements")
        verdict = dict(measurements.get("goal") or {}) if isinstance(measurements, dict) else {}
        path, suffix = _deliverable(attempt)
        resolved = bool(attempt.get("attempted")) and bool(attempt.get("resolved"))
        delivery = {
            "outcome": REPAIRED if resolved else UNREPAIRABLE,
            # THE VERDICT AS MADE, including the `{"profile": ..., "error": ...}` form a verdict
            # that blew up produces: a reader must see that the verdict failed, not a blank.
            "goal": verdict,
            # The bytes the terminal path hands back - "" when there are none, which is what an
            # honest ending on an unrepairable part looks like.
            "path": path,
            "suffix": suffix,
        }
        if not resolved:
            delivery["why"] = _why(attempt)
        logger.info("node_repair_deliver: standalone repair ended %s (file=%s) - job_id=%s",
                    delivery["outcome"], bool(path), state.get("job_id"))
        return {"repair_delivery": delivery}
    except Exception as exc:  # noqa: BLE001 - the ending never raises
        logger.warning("node_repair_deliver: the ending could not be read (%s: %s)",
                       type(exc).__name__, exc)
        return {"repair_delivery": {"outcome": UNREPAIRABLE, "goal": {}, "path": "",
                                    "suffix": "", "why": _NO_REASON}}
