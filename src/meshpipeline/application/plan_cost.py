# Responsibility: NOTE-26, WHAT A TYPED REQUIREMENT COSTS, said at the plan step of an FEA solid: the plan the run
#       would make with every requirement so far (engines/fea_plan_options.requirements_plan - the run's own inputs
#       and planning, no number of its own) against the plan as it stood just before this message, kept beside the
#       checklist's reading so the reply and the count row can say it.
# Boundaries: runs on the geometry worker inside the checklist's reading (application/part_matches.run_reading),
#       never on a request; it stores nothing itself and never fails the reading - what cannot be costed is None.
# Collaborates with: application/part_matches.py (calls it with the reading before this one, keeps the result, shows
#       it), application/fea_check.py (the stage's sizing, its kept plans, the confirmed answers),
#       application/geometry_check.py (the upload, the unit), engines/fea_plan_options.py and engines/fea_driver.py
#       (staging, measuring, the run's planning inputs and planning), engines/meshing_standard.py (the notes read with
#       the team's standard, as the run reads them), agents/intake/plan_talk.py (the plain sentence) and
#       agents/intake/plan_reply.py (the model's facts).
"""WHAT A TYPED REQUIREMENT COSTS (NOTE-26).

A cast knuckle, 25 Nov: "finer at the fillets" took a plan of about 1 million elements to 10 million on the 128 GB
machine - a 96-minute run - and nothing said so before the run. Now, when the checklist's reading of the user's words
lands, the plan the run would make with them is costed as the run would plan it, and kept with the reading.

THE SAME INPUTS AS THE RUN (26 Nov, the C5 knuckle: said "about 0.4 million", built 0.65-0.74 million). The cost
plans with what the run plans with: the notes read with the team's saved standard (engines/meshing_standard, as
fea_driver.read_requirements reads them - the saved growth rate and sizes, and a size typed with a face picked on the
model, NOTE-17, which only that reading carries), the parts reading, the count the checklist shows (NOTE-24, else the
standard's), the size picked on the stage and the stage's kept plans - through fea_driver.requirement_inputs and
fea_driver.size_choice, the run's own first planning. What the run adds after the cost: its AI step's own
refinements (AI-fea-1), decided when it starts.

BEFORE IS THE PLAN AS IT STOOD JUST BEFORE THIS MESSAGE (the same knuckle: "finer at the holes" was told "from 0.9 to
0.4 million" - the first reply's numbers again - because "before" was always the stage's plan with nothing typed and
"after" every requirement so far). Each message gets one reading; "before" is the reading before this one's "after"
(`previous`), so the replies chain: after(1) == before(2) == ... With no reading before (or one that had nothing to
cost) it is the plan the run would make with the team's standard alone; after a reading whose costing failed it is
not known and only the new count is kept.

THE ELEMENT COUNT ONLY, NO MINUTES. The time models the product holds (fea_driver.FEA_USUAL_FILL_RATES, the lab's whole
runs) do not cover the queue, the machine starting, the quality passes the ladder repeats or the reviewer, and the
knuckle's "3 minutes" took 8.5. A number that far off is worse than none, so none is said (the guard refuses a reply
with a number the facts do not hold).

Measured on the test parts (tests/fea_fixtures, 26 Nov, WSL laptop): one planning took 0.06-2.2 s (a 78,000-element
shaft the slowest), so it runs inline in the reading. A big part's plan takes longer; the reply never waits past its
own limit for the reading (NOTE-19: it then comes with the card's poll).
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
#: a reading with nothing to cost keeps this, so the reading after it knows the plan before it was the standard's alone
NOTHING = "nothing"


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


_SIDE_KEYS = ("elements", "standard", "memory_gib", "machine", "where", "cores")


def _side(r: Mapping) -> dict:
    """What is kept of a costed plan: its elements and where it runs (never its minutes - see the module text)."""
    out = {k: r.get(k) for k in _SIDE_KEYS if k in r}
    out["elements"] = int(r.get("elements") or 0)
    out["standard"] = bool(r.get("standard"))
    return out


def target_of(count_rows) -> dict | None:
    """The run's element target from the checklist's count row (NOTE-24), or None."""
    for r in count_rows or ():
        c = (r or {}).get("count") or {}
        n = int(c.get("value") or 0)
        if n:
            return {"elements": n, "mode": _MODES.get(str(c.get("what")), "about"), "quote": "", "source": "notes"}
    return None


def _target(count_rows, asked: Mapping | None) -> dict | None:
    """The count the run aims at: the notes' (the checklist's row), else the team's saved one (STD-1)."""
    from meshpipeline.engines import fea_driver as FD
    return target_of(count_rows) or FD.settings_count(dict(asked) if isinstance(asked, Mapping) else None)


def before_of(previous: Mapping | None) -> tuple[str, dict | None]:
    """Where "before" comes from, given the reading before this one: ("chained", its after) when it was costed;
    ("base", None) when there was none or it had nothing to cost (the plan before is the standard's alone); ("unknown",
    None) when its costing failed. Pure."""
    if not isinstance(previous, Mapping) or previous.get("status") != "done":
        return "base", None
    cost = previous.get("cost")
    if isinstance(cost, Mapping) and int((cost.get("after") or {}).get("elements") or 0) > 0:
        return "chained", dict(cost["after"])
    if isinstance(cost, Mapping) and cost.get("status") == NOTHING:
        return "base", None
    return "unknown", None


def costed(record: dict, surface, diag_m: float, *, before: dict | None, section: Mapping | None,
           asked: Mapping | None = None, count_rows=(), base_asked: Mapping | None = None,
           state: Mapping | None = None, kept: dict | None = None, key: str = "") -> dict:
    """The cost kept with the reading, from a measured part: {"rule", "before" (elements, where it runs; None when
    not known), "after", "factor", "bigger_machine", "seconds"}. `before`: the plan before as a costed row (the
    reading before this one's after), or None - then, given `base_asked`, the plan with the team's standard alone is
    planned here, else "before" is not known. Raises what the planner raises (the caller keeps no cost then)."""
    from meshpipeline.engines import fea_plan_options as PO
    from meshpipeline.engines import mesh_requirements as MR

    t0 = time.time()
    used = dict(section) if isinstance(section, Mapping) and section.get("status") == "used" else None
    asked = dict(asked) if isinstance(asked, Mapping) else MR.section("none")
    run = dict(state or {})
    if before is None and base_asked is not None:
        base = dict(base_asked)
        _rec, plan, _f = PO.requirements_plan(record, surface, diag_m, asked=base, part_sizes=None,
                                              target=_target((), base), state=run, kept=kept, key=key)
        before = PO._costed(_rec, plan, "before")
    got = PO.requirements_cost(record, surface, diag_m, before=before, asked=asked, part_sizes=used,
                               target=_target(count_rows, asked), state=run, kept=kept, key=key)
    return {"rule": RULE, "before": _side(got["before"]) if got.get("before") else None, "after": _side(got["after"]),
            "factor": got.get("factor"), "bigger_machine": bool(got.get("bigger_machine")),
            "seconds": round(time.time() - t0, 2)}


def stage_sized(session_id: str) -> bool:
    """Whether the stage sized this session's FEA part (PLAN-fea-22) - only then is anything costed. Never raises."""
    try:
        from meshpipeline.application import fea_check as FC
        from meshpipeline.application.geometry_check import read_check
        stored = read_check(session_id) or {}
        got = (stored.get("facts") or {}).get(FC.PLAN_FACT_KEY)
        return bool(stored.get("source")) and isinstance(got, Mapping) and bool(got.get("sized"))
    except Exception:  # noqa: BLE001 - not known: not costed
        return False


def _notes_asked(asked: Mapping | None) -> bool:
    """Whether the notes' reading (merged with the standard) holds anything the user typed."""
    from meshpipeline.engines import mesh_requirements as MR
    req = MR.requirements_of(dict(asked)) if isinstance(asked, Mapping) else None
    if not req:
        return False
    return any(r.get("from", "notes") == "notes" for r in req.get("rows") or [])


def wanted(section: Mapping | None, count_rows, asked: Mapping | None = None) -> bool:
    """Whether there is anything to cost: a size or a finer/coarser the parts reading set, a count, or a requirement
    the notes' own reading holds (a size, a growth rate, a size on a picked face)."""
    if count_rows or _notes_asked(asked):
        return True
    return bool(isinstance(section, Mapping) and section.get("status") == "used" and section.get("entries"))


def for_reading(session_id: str, section: Mapping | None, user_notes, *, asked: Mapping | None = None,
                base_asked: Mapping | None = None, previous: Mapping | None = None) -> dict | None:
    """THE WORKER'S STEP: the cost of this reading for the session's FEA part - every requirement so far against the
    plan as it stood before this message (`previous`: the reading before this one, see before_of) - or
    {"status": NOTHING} (nothing to cost), or None (not an FEA part the stage sized, or it could not be costed).
    `asked`, `base_asked`: the notes read with the team's standard, and the standard alone
    (meshing_standard.decide_and_base, then fea_driver.requirements_section). Never raises."""
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
        if not wanted(section, counts, asked):
            return {"rule": RULE, "status": NOTHING}
        if not stage_sized(session_id):
            return None
        from meshpipeline.application import fea_check as FC
        from meshpipeline.application.geometry_check import read_check
        stored = read_check(session_id) or {}
        how, before = before_of(previous)
        state = {"fea_plan_choice": FC.confirmed_plan_choice(session_id)}
        with tempfile.TemporaryDirectory(prefix="plan_cost_") as tmp:
            record, surface, diag_m, key = _measured(session_id, stored, Path(tmp))
            got = costed(record, surface, diag_m, before=before, section=section, asked=asked, count_rows=counts,
                         base_asked=(base_asked if base_asked is not None else {"status": "none"})
                         if how == "base" else None,
                         state=state, kept=FC._kept_plans(session_id), key=key)
        logger.info("plan cost: %s -> %d elements in %.1fs (%s, before %s) - session=%s",
                    (got["before"] or {}).get("elements"), got["after"]["elements"], got["seconds"], RULE, how,
                    session_id)
        return got
    except Exception as exc:  # noqa: BLE001 - no cost said; the run plans and tells it
        logger.warning("plan cost: not costed (%s: %s) - session=%s", type(exc).__name__, str(exc)[:200], session_id)
        return None


def _measured(session_id: str, stored: Mapping, work: Path) -> tuple[dict, Any, float, str]:
    """The uploaded part staged and measured as the run stages it (fea_plan_options.plan_staged: read at the check's
    unit, closed shells as solids, an assembly prepared with the user's answers), and the key the stage's kept plans
    must have been made for (fea_plan_options.plans_key, as fea_driver.drive reads it before anything restages)."""
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
    connection = FD.connection_of({"fea_connection": FC.confirmed_connection(session_id)})
    gap_joins = FC.confirmed_gap_joins(session_id)
    try:
        key = PO.plans_key(ws, connection=connection, gap_joins=gap_joins)
    except Exception:  # noqa: BLE001 - no key: the stage's plans are not reused, as in the run
        key = ""
    FD.solids_from_shells(ws)
    record, surface, diag_m = FD.measure_staged(ws)
    if int(record.get("bodies") or 1) > 1:
        record = FD.prepare_assembly(ws, record, surface, connection=connection,
                                     source_path=str(canonical.path), gap_joins=gap_joins)
    return record, surface, diag_m, key


__all__ = ["NOTHING", "RULE", "WORTH_SAYING", "before_of", "costed", "for_reading", "stage_sized", "target_of",
           "wanted", "worth_saying"]
