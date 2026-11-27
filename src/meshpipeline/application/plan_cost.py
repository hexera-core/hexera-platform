# Responsibility: NOTE-26, WHAT A TYPED REQUIREMENT COSTS, said at the plan step of an FEA solid: the count the run
#       would plan with every requirement so far (`planned` - the run's own inputs, its AI step's decision and its own
#       planning, no number of its own) against the count as it stood just before this message, kept beside the
#       checklist's reading for the count line that follows the reply.
# Boundaries: runs on the geometry worker after the checklist's reading is kept (application/part_matches.run_reading),
#       never on a request and never before the reply; it stores nothing itself and never fails the reading - what
#       cannot be costed is None.
# Collaborates with: application/part_matches.py (calls it with the reading before this one, keeps the result only
#       for the notes it read, hands the AI step's answer to the run), application/fea_check.py (the stage's sizing,
#       its kept plans, the confirmed answers), application/geometry_check.py (the upload, the unit),
#       cad/part_words.py (the reading as the run takes it), engines/fea_ai.py (the AI step), engines/fea_plan_options.py
#       and engines/fea_driver.py (staging, measuring, the run's planning inputs and planning), engines/meshing_standard
#       (the notes read with the team's standard), agents/intake/plan_talk.py (the plain line) and
#       agents/intake/plan_reply.py (count_owed: the line said after the reply).
"""WHAT A TYPED REQUIREMENT COSTS (NOTE-26).

A cast knuckle, 25 Nov: "finer at the fillets" took a plan of about 1 million elements to 10 million on the 128 GB
machine - a 96-minute run - and nothing said so before the run. Now, once the checklist's reading of the user's words
is kept, the count the run would plan with them is worked out as the run would plan it, and said as its own line
after the reply.

THE NUMBER IS THE RUN'S "Estimated before meshing" (the C5 knuckle, 26 Nov: the chat's last count about 0.4 million,
the run's estimate 857,032). Costed with the run's own steps on the run's own inputs: the notes read with the team's
saved standard (as fea_driver.read_requirements reads them, a size on a picked face with them); the parts reading AS
THE RUN TAKES IT (cad/part_words.as_run: each area with its faces - the check's own reading keeps none, and "finer
in/around the holes" was costed as parts named Feature_n that do not exist, so it changed nothing and had no line);
the AI step's decision (engines/fea_ai.decide on the run's brief - asked here, and its answer handed to the run with
the reading, so the run plans with the same one: fea_driver.plan_step_ai); the count the run aims at
(fea_driver.element_target); the size picked on the stage and the stage's kept plans - through
fea_driver.requirement_inputs and fea_driver.size_choice. When the AI step gives no decision here, the run may still
refine, so the count is said as "at least".

BEFORE IS THE COUNT AS IT STOOD JUST BEFORE THIS MESSAGE: the reading before this one's "after" when its cost landed;
else planned here from what that reading was read with (kept with it: `inputs`); with no reading before, the count
the team's standard alone makes. So the lines chain: after(1) == before(2) == ...

THE ELEMENT COUNT ONLY, NO MINUTES. The time models the product holds (fea_driver.FEA_USUAL_FILL_RATES, the lab's whole
runs) do not cover the queue, the machine starting, the quality passes the ladder repeats or the reviewer, and the
knuckle's "3 minutes" took 8.5. A number that far off is worse than none, so none is said.

NEVER BEFORE THE REPLY (the knuckle's replies took 47, 31 and 24 s while the cost was planned inside the reading): the
reading is kept first and answered; the cost lands after it and its line follows as its own message. A cost of notes
that changed meanwhile is never kept, so never said. Measured 26 Nov on a part of the knuckle's shape (11 holes in 4
sizes, about 0.24 million elements, WSL laptop, the model faked): 1.4-2.7 s a costing; the model's own answer for the
AI step comes on top of that.
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
    """The element count the checklist's count row states (NOTE-24), as a target, or None."""
    for r in count_rows or ():
        c = (r or {}).get("count") or {}
        n = int(c.get("value") or 0)
        if n:
            return {"elements": n, "mode": _MODES.get(str(c.get("what")), "about"), "quote": "", "source": "notes"}
    return None


def inputs(section: Mapping | None, asked: Mapping | None, user_notes) -> dict:
    """What one reading costs with, kept with it (part_matches.run_reading) so the reading after it can plan "before"
    from it even when its own costing has not landed: the parts reading, the notes read with the standard, the notes."""
    return {"section": dict(section) if isinstance(section, Mapping) else None,
            "asked": dict(asked) if isinstance(asked, Mapping) else None,
            "user_notes": [dict(n) for n in user_notes or [] if isinstance(n, Mapping)]}


def before_of(previous: Mapping | None) -> tuple[str, Any]:
    """Where "before" comes from, given the reading before this one: ("chained", its after) when its costing landed;
    ("planned", its inputs) when it did not, or had nothing to cost - planned here from what it was read with;
    ("base", None) when there was none (the plan the team's standard alone makes); ("unknown", None) for a reading
    kept before its inputs were. Pure."""
    if not isinstance(previous, Mapping) or previous.get("status") != "done":
        return "base", None
    cost = previous.get("cost")
    if isinstance(cost, Mapping) and int((cost.get("after") or {}).get("elements") or 0) > 0:
        return "chained", dict(cost["after"])
    if isinstance(previous.get("fea_inputs"), Mapping):
        return "planned", dict(previous["fea_inputs"])
    if isinstance(cost, Mapping) and cost.get("status") == NOTHING:
        return "base", None
    return "unknown", None


def _ai(record: dict, diag_m: float, asked: Mapping | None, ai_state: Mapping, *, job_id: str) -> tuple[dict, str]:
    """THE RUN'S AI STEP, asked now (engines/fea_ai.decide, AI-fea-1..9: its refinements, what it sets aside, the count
    it reads from the words) on the brief the run builds - (its section, its answer as given: handed to the run with
    the reading, so the run plans with this same decision). The rules' own plan when it is off or gives none."""
    import asyncio

    from meshpipeline.engines import fea_ai as FA
    from meshpipeline.engines import fea_driver as FD
    from meshpipeline.engines import fea_plan as P

    told: dict = {}
    ai = asyncio.run(FA.decide(record, FA.with_rules_asks(dict(ai_state), asked), P.zones(record), diag_m=diag_m,
                               limit=FD.element_limit(record)[0], job_id=job_id, told=told))
    return ai, (str(told.get("args") or "") if ai.get("status") == "used" else "")


def planned(record: dict, surface, diag_m: float, *, section: Mapping | None, asked: Mapping | None, user_notes,
            ai_state: Mapping, state: Mapping, kept: dict | None, key: str, job_id: str = "") -> dict:
    """THE COUNT THE RUN WOULD PLAN for one set of requirements - the run's own steps with the run's own inputs, and
    nothing else: the reading as the run takes it (cad/part_words.as_run: each area with its faces), the notes read
    with the team's standard (`asked`), the AI step's decision on the run's brief (`_ai`), the count the run aims at
    (fea_driver.element_target, with that decision), and the run's own first planning (fea_plan_options
    .requirements_plan: fea_driver.requirement_inputs + size_choice). The number is the one the run's report prints as
    "Estimated before meshing" (engines/fea_plan: its plan's estimate). Returns {"side": the costed row, "ai_args",
    "ai_used"}."""
    from meshpipeline.cad import part_words as PW
    from meshpipeline.engines import fea_driver as FD
    from meshpipeline.engines import fea_plan_options as PO
    from meshpipeline.engines import mesh_requirements as MR

    asked = dict(asked) if isinstance(asked, Mapping) else MR.section("none")
    used = (PW.as_run(section, record, surface)
            if isinstance(section, Mapping) and section.get("status") == "used" else None)
    st = {**dict(ai_state), "user_notes": [dict(n) for n in user_notes or [] if isinstance(n, Mapping)]}
    ai, args = _ai(record, diag_m, asked, st, job_id=job_id)
    rec, plan, _finer = PO.requirements_plan(record, surface, diag_m, asked=asked, part_sizes=used, ai=ai,
                                             target=FD.element_target(st, ai, asked), state=dict(state), kept=kept,
                                             key=key)
    return {"side": _side(PO._costed(rec, plan, "plan")), "ai_args": args, "ai_used": ai.get("status") == "used"}


def costed(record: dict, surface, diag_m: float, *, before: tuple[str, Any], section: Mapping | None,
           asked: Mapping | None, user_notes, base_asked: Mapping | None, ai_state: Mapping, state: Mapping,
           kept: dict | None = None, key: str = "", job_id: str = "") -> dict:
    """The cost kept with the reading: {"rule", "before" (None when not known), "after", "factor", "bigger_machine",
    "ai_args" (the AI step's answer, for the run), "floor" (the AI step gave no decision now: the run may still
    refine, so the count is said as "at least"), "seconds"}. `before`: before_of's answer. Raises what the planner
    raises."""
    from meshpipeline.engines import mesh_requirements as MR
    from meshpipeline.engines.fea_ai import FEA_AI

    t0 = time.time()
    common: dict[str, Any] = {"ai_state": ai_state, "state": state, "kept": kept, "key": key, "job_id": job_id}
    after = planned(record, surface, diag_m, section=section, asked=asked, user_notes=user_notes, **common)
    how, got = before
    b: dict | None = None
    if how == "chained":
        b = dict(got)
    elif how == "planned":
        b = planned(record, surface, diag_m, section=got.get("section"), asked=got.get("asked"),
                    user_notes=got.get("user_notes"), **common)["side"]
    elif how == "base":
        b = planned(record, surface, diag_m, section=None,
                    asked=base_asked if base_asked is not None else MR.section("none"), user_notes=[],
                    **common)["side"]
    a = after["side"]
    factor = a["elements"] / b["elements"] if b and b.get("elements") else None
    bigger = bool(b) and (float(a.get("memory_gib") or 0.0) > float((b or {}).get("memory_gib") or 0.0)
                          or (bool((b or {}).get("standard")) and not a.get("standard")))
    return {"rule": RULE, "before": b, "after": a, "factor": round(factor, 2) if factor else None,
            "bigger_machine": bool(bigger), "ai_args": after["ai_args"],
            "floor": bool(FEA_AI) and not after["ai_used"], "seconds": round(time.time() - t0, 2)}


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


def _counts(user_notes) -> list[dict]:
    """The count the checklist shows (NOTE-24: the newest note that states one), while the AI step reads the notes."""
    from meshpipeline.agents.intake import plan_talk as PT
    from meshpipeline.contracts.user_notes import own_notes
    from meshpipeline.engines.fea_ai import FEA_AI
    if not FEA_AI:
        return []
    for n in reversed(own_notes(user_notes)):
        rows = PT.count_rows(n, flow=False, note_id=str(n.get("id") or ""))
        if rows:
            return rows
    return []


def wanted(section: Mapping | None, count_rows, asked: Mapping | None = None) -> bool:
    """Whether there is anything to cost: a size or a finer/coarser the parts reading set, a count, or a requirement
    the notes' own reading holds (a size, a growth rate, a size on a picked face)."""
    if count_rows or _notes_asked(asked):
        return True
    return bool(isinstance(section, Mapping) and section.get("status") == "used" and section.get("entries"))


def for_reading(session_id: str, section: Mapping | None, user_notes, *, asked: Mapping | None = None,
                base_asked: Mapping | None = None, previous: Mapping | None = None,
                ai_state: Mapping | None = None) -> dict | None:
    """THE WORKER'S STEP, after the reading is kept (the reply never waits for it): the cost of this reading for the
    session's FEA part - every requirement so far against the plan as it stood before this message (`previous`: the
    reading before this one, see before_of) - or {"status": NOTHING} (nothing to cost), or None (not an FEA part the
    stage sized, or it could not be costed). `asked`, `base_asked`: the notes read with the team's standard, and the
    standard alone (meshing_standard.decide_and_base, then fea_driver.requirements_section). `ai_state`: what the
    run's AI step is shown besides the notes (the request, the owner, the standard). Never raises."""
    try:
        if not wanted(section, _counts(user_notes), asked):
            return {"rule": RULE, "status": NOTHING}
        if not stage_sized(session_id):
            return None
        from meshpipeline.application import fea_check as FC
        from meshpipeline.application.geometry_check import read_check
        stored = read_check(session_id) or {}
        before = before_of(previous)
        state = {"fea_plan_choice": FC.confirmed_plan_choice(session_id)}
        with tempfile.TemporaryDirectory(prefix="plan_cost_") as tmp:
            record, surface, diag_m, key = _measured(session_id, stored, Path(tmp))
            got = costed(record, surface, diag_m, before=before, section=section, asked=asked, user_notes=user_notes,
                         base_asked=base_asked, ai_state=dict(ai_state or {}), state=state,
                         kept=FC._kept_plans(session_id), key=key, job_id=f"cost-{session_id}")
        logger.info("plan cost: %s -> %d elements in %.1fs (%s, before %s) - session=%s",
                    (got["before"] or {}).get("elements"), got["after"]["elements"], got["seconds"], RULE, before[0],
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


__all__ = ["NOTHING", "RULE", "WORTH_SAYING", "before_of", "costed", "for_reading", "inputs", "planned",
           "stage_sized", "target_of", "wanted", "worth_saying"]
