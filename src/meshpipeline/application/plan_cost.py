# Responsibility: NOTE-26, WHAT A TYPED REQUIREMENT COSTS, said at the plan step of an FEA solid: the plan the run
#       would make with the checklist's sizes and count (engines/fea_plan_options.requirements_cost - the run's own
#       sizing and planner, no number of its own) against the plan the stage sized without them, kept beside the
#       checklist's reading so the reply and the count row can say it.
# Boundaries: runs on the geometry worker inside the checklist's reading (application/part_matches.run_reading),
#       never on a request; it stores nothing itself and never fails the reading - what cannot be costed is None.
# Collaborates with: application/part_matches.py (calls it, keeps the result, shows it), application/fea_check.py
#       (the stage's sized plan and the confirmed answers), application/geometry_check.py (the upload, the unit),
#       engines/fea_plan_options.py and engines/fea_driver.py (staging, measuring, planning),
#       agents/intake/plan_talk.py (the plain sentence) and agents/intake/plan_reply.py (the model's facts).
"""WHAT A TYPED REQUIREMENT COSTS (NOTE-26).

A cast knuckle, 25 Nov: "finer at the fillets" took a plan of about 1 million elements to 10 million on the 128 GB
machine - a 96-minute run - and nothing said so before the run. Now, when the checklist's reading of the user's words
lands, the plan the run would make with them is costed as the run would plan it, and kept with the reading:

- the plan before them is the stage's own sized plan (PLAN-fea-22: the user's pick, else the recommended one), kept
  for the run (fea_plan_options.PLANS_FILE) - nothing is planned here when the stage did not size the part;
- the plan after them is fea_plan_options.requirements_cost on the staged solid, with the sizes the reading set
  (the parts reading, AI-fea-9) and the count the notes state (NOTE-24) as the run's target;
- only the numbers are kept: elements, the usual and upper minutes, whether a bigger machine is needed.

Measured on the test parts (tests/fea_fixtures, 26 Nov, WSL laptop): one requirements_cost call took 0.06-2.2 s
(a 78,000-element shaft the slowest), so it runs inline in the reading. A big part's plan takes longer; the reply
never waits past its own limit for the reading (NOTE-19: it then comes with the card's poll).
"""
from __future__ import annotations

import logging
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

RULE = "NOTE-26"
#: the change is said when the element count moves by more than this share of the plan before (or the plan needs a
#: bigger machine): a quarter - smaller moves are inside what the estimate itself can be off by
WORTH_SAYING = 0.25
#: the count's bound as the run's target takes it (engines/fea_driver.element_target's modes)
_MODES = {"ceiling": "at_most", "floor": "at_least", "target": "about"}


def worth_saying(cost: Mapping | None) -> bool:
    """Whether a cost is worth telling the user: the element count moves by more than WORTH_SAYING, or the plan
    needs a bigger machine. Pure."""
    if not isinstance(cost, Mapping):
        return False
    b = int((cost.get("before") or {}).get("elements") or 0)
    a = int((cost.get("after") or {}).get("elements") or 0)
    if b <= 0 or a <= 0:
        return False
    return abs(a - b) > WORTH_SAYING * b or bool(cost.get("bigger_machine"))


def _side(r: Mapping) -> dict:
    return {"elements": int(r.get("elements") or 0), "minutes": int(r.get("minutes") or 0),
            "usual_minutes": int(r.get("usual_minutes") or 0), "standard": bool(r.get("standard"))}


def target_of(count_rows) -> dict | None:
    """The run's element target from the checklist's count row (NOTE-24), or None."""
    for r in count_rows or ():
        c = (r or {}).get("count") or {}
        n = int(c.get("value") or 0)
        if n:
            return {"elements": n, "mode": _MODES.get(str(c.get("what")), "about"), "quote": "", "source": "notes"}
    return None


def costed(record: dict, surface, diag_m: float, *, before: dict, section: Mapping | None,
           count_rows=()) -> dict:
    """The cost kept with the reading, from a measured part and the plan before: {"rule", "before", "after" (each
    elements, minutes, usual_minutes, standard), "factor", "bigger_machine", "seconds"}. Raises what the planner
    raises (the caller keeps no cost then)."""
    from meshpipeline.engines import fea_plan_options as PO
    from meshpipeline.engines import mesh_requirements as MR

    t0 = time.time()
    used = dict(section) if isinstance(section, Mapping) and section.get("status") == "used" else None
    got = PO.requirements_cost(record, surface, diag_m, before=before, asked=MR.section("none"), part_sizes=used,
                               target=target_of(count_rows))
    return {"rule": RULE, "before": _side(got["before"]), "after": _side(got["after"]), "factor": got.get("factor"),
            "bigger_machine": bool(got.get("bigger_machine")), "seconds": round(time.time() - t0, 2)}


def _before_plan(session_id: str, facts: Mapping) -> dict | None:
    """The plan the stage sized and kept for the run (the user's pick, else the recommended one), or None."""
    from meshpipeline.application import fea_check as FC

    got = facts.get(FC.PLAN_FACT_KEY)
    if not isinstance(got, Mapping) or not got.get("sized"):
        return None
    pick = FC.confirmed_plan_choice(session_id) or str(got.get("recommended") or FC.PLAN_FULL)
    plans = FC._kept_plans(session_id)
    plan = plans.get(pick) if isinstance(plans.get(pick), dict) else plans.get(FC.PLAN_FULL)
    return plan if isinstance(plan, dict) and (plan.get("estimate") or {}).get("elements") else None


def wanted(section: Mapping | None, count_rows) -> bool:
    """Whether there is anything to cost: a size or a finer/coarser the reading set, or a count."""
    if count_rows:
        return True
    return bool(isinstance(section, Mapping) and section.get("status") == "used" and section.get("entries"))


def for_reading(session_id: str, section: Mapping | None, user_notes) -> dict | None:
    """THE WORKER'S STEP: the cost of the checklist's sizes and count for the session's FEA part, or None (nothing to
    cost, not an FEA part the stage sized, or it could not be costed). Never raises."""
    from meshpipeline.agents.intake import plan_talk as PT
    from meshpipeline.contracts.user_notes import own_notes

    try:
        from meshpipeline.engines.fea_ai import FEA_AI
        counts: list[dict] = []
        if FEA_AI:
            # the count the checklist shows (NOTE-24: the newest note that states one), as the run's target
            for n in reversed(own_notes(user_notes)):
                counts = PT.count_rows(n, flow=False, note_id=str(n.get("id") or ""))
                if counts:
                    break
        if not wanted(section, counts):
            return None
        from meshpipeline.application.geometry_check import read_check
        stored = read_check(session_id) or {}
        facts = stored.get("facts") or {}
        before = _before_plan(session_id, facts)
        if before is None or not stored.get("source"):
            return None
        with tempfile.TemporaryDirectory(prefix="plan_cost_") as tmp:
            record, surface, diag_m = _measured(session_id, stored, Path(tmp))
            got = costed(record, surface, diag_m, before=before, section=section, count_rows=counts)
        logger.info("plan cost: %d -> %d elements in %.1fs (%s) - session=%s", got["before"]["elements"],
                    got["after"]["elements"], got["seconds"], RULE, session_id)
        return got
    except Exception as exc:  # noqa: BLE001 - no cost said; the run plans and tells it
        logger.warning("plan cost: not costed (%s: %s) - session=%s", type(exc).__name__, str(exc)[:200], session_id)
        return None


def _measured(session_id: str, stored: Mapping, work: Path) -> tuple[dict, Any, float]:
    """The uploaded part staged and measured as the stage's sizing staged it (fea_plan_options.plan_staged: read at
    the check's unit, closed shells as solids, an assembly prepared with the user's answers)."""
    from meshpipeline.application import fea_check as FC
    from meshpipeline.application.geometry_check import _fetch
    from meshpipeline.cad.ingest import canonicalise
    from meshpipeline.contracts.geometry_source import GeometrySourceRef
    from meshpipeline.contracts.intake_formats import GeometryKind
    from meshpipeline.engines import fea_driver as FD
    from meshpipeline.engines import fea_plan_options as PO

    ref = GeometrySourceRef.from_payload(stored["source"])
    canonical = canonicalise(_fetch(ref, work), work, stem="geometry", ecxml_form="placed")
    if canonical.kind is not GeometryKind.cad:
        raise ValueError("not a CAD solid")
    ws = work / "staged"
    ws.mkdir()
    PO.stage_file(canonical.path, ws, to_metres=float((stored.get("facts") or {}).get("scale_to_m") or 0.001))
    FD.solids_from_shells(ws)
    record, surface, diag_m = FD.measure_staged(ws)
    if int(record.get("bodies") or 1) > 1:
        record = FD.prepare_assembly(ws, record, surface, connection=FC.confirmed_connection(session_id),
                                     source_path=str(canonical.path), gap_joins=FC.confirmed_gap_joins(session_id))
    return record, surface, diag_m


__all__ = ["RULE", "WORTH_SAYING", "costed", "for_reading", "target_of", "wanted", "worth_saying"]
