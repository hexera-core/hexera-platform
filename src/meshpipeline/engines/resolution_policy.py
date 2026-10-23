# Responsibility: Decide what a SOUND mesh that misses one of our own resolution or size gates becomes (a note, not a failure), and the fixed lever ladder a retry climbs to close the gap.
# Owns: the PLAN-cfd and LAD-cfd rules (docs/RULES.md), the rung a run is on and its workspace fact, the note's facts and plain words, and the hook for the big-machine runner (route_big).
# Boundaries: engine-neutral and pure over plain facts and one small workspace file. It meshes nothing and judges no mesh: each engine's gates measure, each engine applies the rung to its own sizing, and the executor, the router and terminal assembly call the functions here.
# Collaborates with: pipeline/executor (gate failures that are notes), pipeline/graph (whether a rung is left), pipeline/engine_fallback (climbs or resets the rung), agents/builder/workspace (writes the fact), the cfMesh / VMTK / gmsh / snappy sizing (read it), application/final_result (says the note).
"""A SOUND MESH IS DELIVERED; OUR OWN BARS BECOME NOTES; A RETRY ALWAYS CHANGES SOMETHING.

THE PROBLEM (the CFD failure census, 2026-10-23). Of 86 failed clean dev files, 38 had a mesh in
hand with no fatal checkMesh error. Our own gates threw them away: the resolution floor (fewer than
12 cells across the narrowest 5% of the wall), the 8 M one-job cell limit, the share of skewed
faces. And the retry after such a gate was either an AI guess or a no-op ("authored mesh spec
unchanged ... refusing an identical re-run", 18 runs) - never a rule.

THE RULES (docs/RULES.md, PLAN-cfd and LAD-cfd):

* PLAN-cfd-1 - cells are sized from the measured narrowest gap, locally, on every flow engine
  (snappyHexMesh and cfMesh: engines/gap_sizing boxes; VMTK: edge length from the local radius;
  gmsh: the passage size field). Cited where each engine sizes.
* PLAN-cfd-2 - every opening gets at least PORT_CELLS_ACROSS cells across its own width, locally.
* PLAN-cfd-3 - THE BIG-MACHINE HOOK. When the cells the sizing rule needs are over one job's cell
  limit, the plan says `route_big` (gap_sizing.GapPlan.record). The big-machine runner is not built
  yet: until it is, the run is meshed as the limit holds and says so.
* LAD-cfd-1 - THE RESOLUTION LADDER. A sound mesh short of the floor sends the next attempt up one
  rung: the cells-across target at the narrow spots is multiplied by the rung's factor (1.5, then
  2), or by what the measured shortfall needs ((floor + 1) / measured) when that is more, never
  past RUNG_CAP. The rung is written into the attempt's workspace (LADDER_FILE), so the case the
  mesher gets is never the one it already ran (agents/builder/rerun_guard hashes the file).
* LAD-cfd-2 - A SWITCH DOES NOT SPEND THE LADDER. When the run moved to another engine and that
  engine's first mesh is sound but short, it still gets one rung, within the run's ceiling of
  attempts (BUILDER_MAX_TOTAL_ATTEMPTS) and only when a whole attempt still fits in the run's time
  (a guard clock, recorded as one).
* LAD-cfd-3 - LADDER SPENT, MESH SOUND: DELIVERED WITH A NOTE naming the narrowest gap, where it is,
  its cells across and the floor. A later attempt that comes out broken never throws an earlier
  sound mesh away: the run delivers the newest sound one (route `node_deliver_sound`).
* LAD-cfd-4 - OVER THE ONE-JOB CELL LIMIT IS A NOTE on a sound mesh, never a retry: a retry could
  only shrink a mesh the rules asked for.

What still blocks: anything that makes the mesh invalid or the wrong problem - checkMesh's fatal
defects, negative volumes, a port with no faces, the wrong region, a domain that is not the one
asked. Those gates stay blocking; only the gates named in NOTE_GATES become notes.
"""
from __future__ import annotations

import json
import logging
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# the rule IDs (docs/RULES.md); cited where each decision is made
PLAN_GAP_LOCAL = "PLAN-cfd-1"
PLAN_PORTS = "PLAN-cfd-2"
PLAN_ROUTE_BIG = "PLAN-cfd-3"
LAD_RUNG = "LAD-cfd-1"
LAD_AFTER_SWITCH = "LAD-cfd-2"
LAD_DELIVER_SOUND = "LAD-cfd-3"
LAD_CELL_LIMIT = "LAD-cfd-4"

#: PLAN-cfd-2: cells across every opening's own width. Under about 4 the octree seals a port over;
#: 8 carries a velocity profile through it (the census's ask, bucket 1b).
PORT_CELLS_ACROSS = 8

#: LAD-cfd-1: the factor on the narrow spots' cells-across target, per rung (rung 0 = the plan).
RUNG_FACTORS: tuple[float, ...] = (1.0, 1.5, 2.0)
#: ...and the most any rung may multiply it by, whatever the measured shortfall asks.
RUNG_CAP = 3.0

#: The workspace fact a rung travels in (written by the builder from the run's state, read by the
#: engines' sizing, part of the case the rerun guard hashes).
LADDER_FILE = "resolution_ladder.json"

#: The state key the run's ladder lives in.
STATE_KEY = "resolution_ladder"
#: The state key the sound meshes of a run are listed in (LAD-cfd-3).
SOUND_KEY = "sound_attempts"

#: The caveat kinds a sound mesh is delivered with (application/final_result renders them).
RESOLUTION_SHORT = "resolution_short"
OVER_CELL_LIMIT = "over_cell_limit"
NOTE_KINDS = (RESOLUTION_SHORT, OVER_CELL_LIMIT)

#: THE GATES THAT ARE NOTES on a sound mesh: gate key -> the caveat kind it becomes. Declared on
#: each engine's GateSpec as blocking=False; listed here so the executor and the rulebook agree.
NOTE_GATES: dict[str, str] = {"resolution_floor": RESOLUTION_SHORT,
                              "cell_limit": OVER_CELL_LIMIT}


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


# #
# the rung
# #

def rung_factor(rung: int, *, measured: float | None = None, floor: float | None = None) -> float:
    """LAD-cfd-1: the factor on the narrow spots' cells-across target at `rung`. Rung 0 is the
    plan (1). A higher rung takes its own factor, or what the measured shortfall needs when that is
    more ((floor + 1) / measured - the same arithmetic the snappy driver's narrow retry uses),
    never past RUNG_CAP."""
    r = max(0, int(rung or 0))
    if r == 0:
        return 1.0
    base = RUNG_FACTORS[min(r, len(RUNG_FACTORS) - 1)]
    m, f = _num(measured), _num(floor)
    need = (f + 1.0) / m if m and f and m > 0.0 else 0.0
    return round(min(RUNG_CAP, max(base, need)), 3)


def ladder_of(state: Mapping) -> dict:
    """The run's ladder record: {"rung": the rung the current attempt was built at, "engine": the
    engine it belongs to, "switched": the run moved engines at least once, "after_switch": the
    LAD-cfd-2 rung was granted, "measured"/"floor": the shortfall the rung answers}."""
    raw = state.get(STATE_KEY) if isinstance(state, Mapping) else None
    return dict(raw) if isinstance(raw, Mapping) else {}


def current_rung(state: Mapping) -> int:
    try:
        return max(0, int(ladder_of(state).get("rung") or 0))
    except (TypeError, ValueError):
        return 0


def fact_for(state: Mapping) -> dict | None:
    """The workspace fact for the attempt about to be built, or None at rung 0 (the plan as is:
    no file, so a case built at rung 0 is byte for byte what it was before the ladder existed)."""
    lad = ladder_of(state)
    rung = current_rung(state)
    if rung <= 0:
        return None
    return {"rung": rung, "factor": float(lad.get("factor") or rung_factor(rung)),
            "rule": LAD_RUNG, "measured": lad.get("measured"), "floor": lad.get("floor"),
            "engine": lad.get("engine") or str(state.get("engine") or "")}


def write_fact(workspace, state: Mapping) -> None:
    """Write (or clear) the attempt's rung beside its case. Never raises."""
    try:
        p = Path(workspace) / LADDER_FILE
        fact = fact_for(state)
        if fact is None:
            p.unlink(missing_ok=True)
        else:
            p.write_text(json.dumps(fact, sort_keys=True), encoding="utf-8")
    except OSError:
        logger.warning("resolution ladder: the rung could not be written", exc_info=True)


def read_fact(workspace) -> dict:
    """The rung this attempt is built at ({} = rung 0, the plan as is)."""
    try:
        d = json.loads((Path(workspace) / LADDER_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return d if isinstance(d, dict) else {}


def factor_of(workspace) -> float:
    """The factor this attempt's narrow spots are sized up by (1.0 at rung 0)."""
    f = _num(read_fact(workspace).get("factor"))
    return f if f is not None and f >= 1.0 else 1.0


# #
# the shared note gates
# #

#: What the cell-limit gate proves, in the engineer's words (GateSpec.proves).
CELL_LIMIT_PROVES = ("The mesh fits one standard job's cell limit (a bigger sound mesh is still "
                     "delivered, with a note)")


def gate_cell_limit(ctx) -> tuple[bool, str]:
    """LAD-cfd-4: the delivered mesh against one job's cell limit. Declared blocking=False on every
    flow engine: over the limit, a sound mesh is delivered with the note - the size is what the
    geometry needed, and a retry could only shrink it. It used to sit inside each engine's manifest
    check and fail the run ("the mesh is TOO FINE. COARSEN it"; 7 dev files of the census)."""
    import meshpipeline.settings.policy as polcfg
    from meshpipeline.contracts.failure_cause import FailureCause
    from meshpipeline.engines.gates import refuse
    n = _num((ctx.manifest_or_load() or {}).get("cell_count"))
    limit = int(polcfg.CELL_HARD_LIMIT)
    if n is None or n <= limit:
        return True, ""
    return False, refuse(
        f"[CELL_LIMIT] the mesh has {int(n):,} cells, over one standard job's {limit:,}. It is "
        "delivered as it is when it is otherwise sound (a note, not a failure): the size is what "
        "the geometry's sizing rules asked for.",
        FailureCause.CELL_BUDGET, cells=int(n), limit=limit)


# #
# the note a gate becomes
# #

def is_note_gate(gate_key: str) -> bool:
    return str(gate_key or "") in NOTE_GATES


def _narrowest_region(quality: Mapping) -> dict | None:
    """The measured region with the fewest cells across (engines/passage.under_floor_regions)."""
    uf = quality.get("passage_under_floor") if isinstance(quality, Mapping) else None
    regions = uf.get("regions") if isinstance(uf, Mapping) else None
    best = None
    for g in regions or ():
        if not isinstance(g, Mapping):
            continue
        c = _num(g.get("cells"))
        if c is None:
            continue
        if best is None or c < float(best["cells"]):
            best = g
    if best is None:
        return None
    try:
        centre = [round(0.5 * (float(a) + float(b)), 6) for a, b in zip(best["min"], best["max"],
                                                                          strict=True)]
    except (KeyError, TypeError, ValueError):
        centre = None
    r = _num(best.get("radius_m"))
    return {"cells_across": _num(best.get("cells")), "at_m": centre,
            "width_m": (2.0 * r if r is not None else None)}


def caveat(gate_key: str, facts: Mapping | None, manifest: Mapping | None, *,
           engine: str = "") -> dict:
    """The requirement caveat a note gate's failure becomes on a sound mesh: the gate's own facts,
    where the narrowest gap is (from the measure beside the mesh), and the rule that delivers it."""
    facts = dict(facts or {})
    q = (manifest or {}).get("quality") if isinstance(manifest, Mapping) else None
    q = q if isinstance(q, Mapping) else {}
    # every engine's note carries the mesh's size against the limit (the snappy gate already
    # states them; the others do not), so the ladder can tell whether a rung has room
    import meshpipeline.settings.policy as polcfg
    if _num(facts.get("cells")) is None and isinstance(manifest, Mapping):
        n = _num(manifest.get("cell_count"))
        if n is not None:
            facts["cells"] = int(n)
    if _num(facts.get("cell_limit")) is None:
        facts["cell_limit"] = int(polcfg.CELL_HARD_LIMIT)
    kind = NOTE_GATES.get(str(gate_key or ""), RESOLUTION_SHORT)
    out: dict[str, Any] = {"kind": kind, "gate": str(gate_key or ""), "engine": engine,
                           "rule": LAD_DELIVER_SOUND if kind == RESOLUTION_SHORT else LAD_CELL_LIMIT}
    if kind == RESOLUTION_SHORT:
        out.update({"cells_across": _num(facts.get("cells_across")),
                    "floor": _num(facts.get("needed")),
                    "scope": facts.get("scope") or "",
                    "cells": _num(facts.get("cells")),
                    "cell_limit": _num(facts.get("cell_limit")),
                    "rebuild_cells": _num(facts.get("rebuild_cells"))})
        where = _narrowest_region(q)
        if where:
            out["narrowest"] = where
        # the facts the snappy driver's narrow retry reads back (cells_across, needed, cells)
        out["facts"] = {k: v for k, v in facts.items()
                        if isinstance(v, (int, float, str, bool)) or v is None}
    else:
        out.update({"cells": _num(facts.get("cells")),
                    "limit": _num(facts.get("limit")) or _num(facts.get("cell_limit"))})
    return out


def note_caveats(caveats) -> list[dict]:
    return [dict(c) for c in caveats or () if isinstance(c, Mapping) and c.get("kind") in NOTE_KINDS]


def resolution_caveat(state_or_caveats) -> dict | None:
    """The resolution note of the attempt the state holds (or among `caveats`), or None."""
    cav = (state_or_caveats.get("requirement_caveats")
           if isinstance(state_or_caveats, Mapping) else state_or_caveats)
    for c in cav or ():
        if isinstance(c, Mapping) and c.get("kind") == RESOLUTION_SHORT:
            return dict(c)
    return None


def under_resolved_facts(state: Mapping) -> dict | None:
    """The resolution floor's facts for the attempt the state holds - from a failed gate (the old
    blocking floor, still how an older record reads) or from its note on a sound mesh - else None.
    What the snappy driver's narrow retry reads."""
    from meshpipeline.contracts.failure_cause import FailureCause
    if str(state.get("executor_failure_cause") or "") == FailureCause.UNDER_RESOLVED.value:
        facts = state.get("executor_failure_facts")
        return dict(facts) if isinstance(facts, Mapping) and facts else None
    if state.get("executor_success"):
        c = resolution_caveat(state)
        if c and isinstance(c.get("facts"), Mapping) and c["facts"]:
            return dict(c["facts"])
    return None


# #
# the router's questions
# #

#: The share of the one-job cell limit past which a rung has no room left to refine (_over_limit).
ROOM_SHARE = 0.9


def _over_limit(c: Mapping) -> bool:
    """No room left to refine: the mesh already has ROOM_SHARE of the one-job cell limit, so a rung
    could only come back as the same mesh (each engine's gap plan holds the limit). Then the note
    is the answer at once. The gate's own whole-part estimate (rebuild_cells) is not used: the
    ladder refines the narrow spots only, which costs far less than refining every wall cell."""
    cells, limit = _num(c.get("cells")), _num(c.get("cell_limit"))
    if cells is None or limit is None or limit <= 0:
        return False
    return cells >= ROOM_SHARE * limit


def rung_left(state: Mapping) -> bool:
    """Whether the ladder has a rung above the one the current attempt was built at."""
    return current_rung(state) + 1 < len(RUNG_FACTORS)


def retry_can_change(caveats, state: Mapping) -> bool:
    """Whether a retry can change any of these caveats: a domain-extent near-miss (as always), or a
    resolution note while a rung is left and a rebuild reaching the floor fits the limit. A cell
    limit note never: a retry could only shrink a mesh the rules asked for (LAD-cfd-4)."""
    for c in caveats or ():
        if not isinstance(c, Mapping):
            continue
        kind = c.get("kind") or "domain_extent"
        if kind == RESOLUTION_SHORT:
            if rung_left(state) and not _over_limit(c):
                return True
        elif kind not in NOTE_KINDS:
            return True
    return False


def switched(state: Mapping) -> bool:
    """Whether the run moved to another engine at least once (its ladder record says so)."""
    rec = state.get("engine_ladder") if isinstance(state, Mapping) else None
    if isinstance(rec, Mapping) and rec.get("switches"):
        return True
    return bool(ladder_of(state).get("switched"))


def rung_after_switch(state: Mapping, *, attempts_ceiling: int, attempt_seconds: float,
                      now: float | None = None) -> tuple[bool, str]:
    """LAD-cfd-2: (whether the run gets one more attempt for a rung, why not in words). Only for a
    resolution note on the engine the run switched to, at its first rung, inside the run's ceiling
    of attempts, and only when a whole attempt still fits in the run's time (a guard clock: a rung
    that ran the run out of time would lose the sound mesh it has)."""
    import time as _time
    c = resolution_caveat(state)
    if c is None or not switched(state):
        return False, "no switch"
    if current_rung(state) != 0 or ladder_of(state).get("after_switch"):
        return False, "this engine already climbed a rung"
    if not rung_left(state) or _over_limit(c):
        return False, "no rung fits"
    if int(state.get("retry_count", 0) or 0) >= int(attempts_ceiling):
        return False, "the run's ceiling of attempts is reached"
    deadline = _num(state.get("pipeline_deadline_epoch"))
    if deadline is not None:
        left = deadline - (now if now is not None else _time.time())
        if left < float(attempt_seconds):
            from meshpipeline.engines.determinism import note_time_limit
            note_time_limit("resolution_rung_after_switch", "the rung an engine switch leaves "
                            "was not started: a whole attempt no longer fits in the run's time",
                            left_s=left, needed_s=float(attempt_seconds))
            return False, "too little time left for a whole attempt"
    return True, ""


def next_ladder(state: Mapping, *, switch_to: str = "") -> dict:
    """The ladder record for the attempt about to start: the rung reset on a switch (the new engine
    plans its own first mesh - a switch spends no rung), one rung up after a resolution note or an
    under-resolved failure on the same engine, unchanged otherwise."""
    lad = ladder_of(state)
    engine = str(state.get("engine") or "")
    if switch_to and switch_to != engine:
        return {"rung": 0, "engine": switch_to, "switched": True}
    facts = under_resolved_facts(state)
    if facts is None:
        lad.setdefault("engine", engine)
        return lad
    rung = min(current_rung(state) + 1, len(RUNG_FACTORS) - 1)
    measured, floor = _num(facts.get("cells_across")), _num(facts.get("needed"))
    out = {**lad, "rung": rung, "engine": engine,
           "factor": rung_factor(rung, measured=measured, floor=floor),
           "measured": measured, "floor": floor}
    if lad.get("switched") and current_rung(state) == 0 and int(state.get("retry_count", 0) or 0) > 1:
        out["after_switch"] = True
    return out


# #
# the sound meshes of a run (LAD-cfd-3)
# #

def sound_entry(state: Mapping, *, workspace: str, manifest: Mapping | None,
                caveats: list) -> dict:
    """One sound mesh of this run, as the router may deliver it later."""
    return {"attempt": int(state.get("retry_count", 0) or 0), "workspace": str(workspace),
            "engine": str((manifest or {}).get("mesh_mode") or state.get("engine") or ""),
            "engine_params": dict(state.get("engine_params") or {}),
            "requirement_caveats": [dict(c) for c in caveats or () if isinstance(c, Mapping)]}


def with_sound(state: Mapping, entry: dict) -> list[dict]:
    rows = [dict(r) for r in (state.get(SOUND_KEY) or ()) if isinstance(r, Mapping)]
    rows = [r for r in rows if r.get("workspace") != entry.get("workspace")]
    rows.append(entry)
    return rows


def deliverable_sound(state: Mapping) -> dict | None:
    """The sound mesh the run delivers when its last attempt left none (LAD-cfd-3): the newest one
    whose manifest still reads with no fatal defect. None when there is none."""
    for row in reversed([r for r in (state.get(SOUND_KEY) or ()) if isinstance(r, Mapping)]):
        ws = str(row.get("workspace") or "")
        if not ws:
            continue
        try:
            manifest = json.loads((Path(ws) / "mesh_manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        q = manifest.get("quality") if isinstance(manifest, dict) else None
        if isinstance(q, dict) and q.get("fatal"):
            continue
        return {**dict(row), "manifest": manifest}
    return None


# #
# the why record
# #

def cite_plan(job_id: str, workspace, *, attempt: int) -> None:
    """The attempt's narrow-gap plan in the run's why record (PLAN-cfd-1), and the big-machine
    hook when the rule's mesh is over one job's limit (PLAN-cfd-3) - read from the plan record the
    engine wrote beside its case (engines/gap_sizing.GAP_RECORD). Never raises."""
    try:
        from meshpipeline.contracts import decision_record as dr
        from meshpipeline.engines.gap_sizing import read_record
        rec = read_record(workspace)
        if not rec:
            return
        inputs = {k: rec.get(k) for k in ("status", "spots", "narrowest_m", "needed", "limit",
                                          "rule_cells_across", "target")}
        dr.cite(job_id, stage="plan", decided="narrow gaps", value=rec.get("status"),
                rule=PLAN_GAP_LOCAL, inputs={"attempt": attempt, **inputs},
                words=(f"attempt {attempt}: {rec.get('spots')} narrow spot(s) sized locally to "
                       f"{rec.get('target')} cells across ({rec.get('status')})"))
        if rec.get("route_big"):
            dr.cite(job_id, stage="plan", decided="route_big", value=True, rule=PLAN_ROUTE_BIG,
                    inputs={"attempt": attempt, "needed": rec.get("needed"),
                            "limit": rec.get("limit")},
                    words=(f"the sizing rule needs about {rec.get('needed')} cells, over one "
                           f"job's {rec.get('limit')}: this mesh belongs on a bigger machine "
                           "(not built yet - meshed as the limit holds)"))
    except Exception:  # noqa: BLE001 - a record never changes a run
        logger.debug("resolution policy: plan not cited", exc_info=True)


# #
# the words
# #

def _mm(metres: float | None) -> str | None:
    if metres is None:
        return None
    v = float(metres) * 1000.0
    return f"{v:.2g} mm" if v < 10 else f"{v:.0f} mm"


def note_lines(c: Mapping) -> list[str]:
    """The note in plain words, one fact a line."""
    kind = c.get("kind")
    if kind == OVER_CELL_LIMIT:
        cells, limit = _num(c.get("cells")), _num(c.get("limit"))
        return [f"  - The mesh has {cells / 1e6:.1f} M cells, more than the {limit / 1e6:.0f} M "
                "one standard job is sized for." if cells and limit else
                "  - The mesh has more cells than one standard job is sized for.",
                "    It is sound, and it is the size your geometry needs, so it is delivered as "
                "it is. Make sure your solver machine has the memory for it."]
    x, floor = _num(c.get("cells_across")), _num(c.get("floor")) or 12.0
    near = c.get("narrowest") if isinstance(c.get("narrowest"), Mapping) else {}
    at = near.get("at_m") if isinstance(near, Mapping) else None
    width = _mm(_num(near.get("width_m"))) if isinstance(near, Mapping) else None
    head = "  - The narrowest gap"
    if width:
        head += f" (about {width} across"
        if at:
            head += ", at (" + ", ".join(f"{float(v) * 1000:.0f}" for v in at) + ") mm"
        head += ")"
    lines = [head + (f" has about {x:.1f} cells across it. Our floor is {floor:g}, and 20 to 40 "
                     "is common practice." if x is not None else
                     f" has fewer cells across it than our floor of {floor:g}.")]
    if x is not None and x > 0:
        lines.append(f"    That is {max(0.0, floor - x):.1f} cells short ({x / floor * 100:.0f}% "
                     "of the floor) at the narrowest 5% of the wall; the rest of the mesh is "
                     "finer.")
    lines.append("    The mesh is sound - no broken cells - so it is delivered. If the flow in "
                 "that gap matters to you, ask for a finer mesh there.")
    return lines


__all__ = ["CELL_LIMIT_PROVES", "ROOM_SHARE", "cite_plan",
           "gate_cell_limit", "LADDER_FILE", "LAD_AFTER_SWITCH", "LAD_CELL_LIMIT", "LAD_DELIVER_SOUND", "LAD_RUNG",
           "NOTE_GATES", "NOTE_KINDS", "OVER_CELL_LIMIT", "PLAN_GAP_LOCAL", "PLAN_PORTS",
           "PLAN_ROUTE_BIG", "PORT_CELLS_ACROSS", "RESOLUTION_SHORT", "RUNG_CAP", "RUNG_FACTORS",
           "SOUND_KEY", "STATE_KEY", "caveat", "current_rung", "deliverable_sound", "fact_for",
           "factor_of", "is_note_gate", "ladder_of", "next_ladder", "note_caveats", "note_lines",
           "read_fact", "resolution_caveat", "retry_can_change", "rung_after_switch",
           "rung_factor", "rung_left", "sound_entry", "switched", "under_resolved_facts",
           "with_sound", "write_fact"]
