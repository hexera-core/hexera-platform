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
* PLAN-cfd-3 - THE BIG-MACHINE HOOK. When the cells the sizing rule needs are over the run's cell
  limit, the plan says `route_big` (gap_sizing.GapPlan.record) and the run is meshed as the limit
  holds and says so - with no question (Areen, 14 Nov). The big machine is the user's to ask for by
  a count in their notes: past the standard machine's cells it runs on a VM that carries them
  (RUN-9, contracts/mesh_machine.route_cells).
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
    # RUN-9: the machine it was meshed on, when that was a bigger one sized for the count asked for
    # (the note then only says the size); still a note, so the worker never runs its trial solve
    try:
        from meshpipeline.contracts import mesh_machine as MM
        carried = MM.cells_carried(MM.read(ctx.workspace))
    except Exception:  # noqa: BLE001 - an unread machine is the standard one
        carried = limit
    return False, refuse(
        f"[CELL_LIMIT] the mesh has {int(n):,} cells, over one standard job's {limit:,}. It is "
        "delivered as it is when it is otherwise sound (a note, not a failure): the size is what "
        "the geometry's sizing rules, or the user's own count, asked for.",
        FailureCause.CELL_BUDGET, cells=int(n), limit=limit,
        **({"machine_cells": int(carried)} if carried > limit else {}))


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
           engine: str = "", plan: Mapping | None = None) -> dict:
    """The requirement caveat a note gate's failure becomes on a sound mesh: the gate's own facts,
    where the narrowest gap is (from the measure beside the mesh), the attempt's narrow-gap plan
    (`plan`: engines/gap_sizing's record, when the engine wrote one) and the rule that delivers
    it."""
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
        if isinstance(plan, Mapping) and plan.get("status"):
            # whether the plan had room: a plan the cell limit already held leaves a rung nothing
            # to add (_no_room), and its route_big says where the full rule's mesh belongs
            out["plan"] = {"status": str(plan.get("status")),
                           "route_big": bool(plan.get("route_big")),
                           "needed": _num(plan.get("needed")),
                           "affordable": _num(plan.get("affordable")),
                           "budget": _num(plan.get("budget")),
                           "limit": _num(plan.get("limit"))}
        # the facts the snappy driver's narrow retry reads back (cells_across, needed, cells)
        out["facts"] = {k: v for k, v in facts.items()
                        if isinstance(v, (int, float, str, bool)) or v is None}
    else:
        out.update({"cells": _num(facts.get("cells")),
                    "limit": _num(facts.get("limit")) or _num(facts.get("cell_limit"))})
        if _num(facts.get("machine_cells")) is not None:
            out["machine_cells"] = _num(facts.get("machine_cells"))
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


#: The gap-plan statuses (engines/gap_sizing) that mean the cell limit, or the user's own choice of
#: fewer cells, already decided how many cells the narrow spots get: a rung re-plans the same cells.
HELD_PLANS = ("limited", "over_limit", "accepted_fewer")


def _over_limit(c: Mapping) -> bool:
    """No room left to refine, so a rung could only come back as the same mesh. Then the note is the
    answer at once. Two readings say it:
    - the mesh already has ROOM_SHARE of the one-job cell limit;
    - the attempt's gap plan was held (HELD_PLANS) with its budget already the whole one-job limit:
      the next plan has the same cells to share, so it shares them the same way. On the lab
      (gr-br2, 2026-10-23) every such rung rebuilt the same mesh, to the cell (7 of 7, cfMesh).
      A held plan under a smaller budget keeps its rung - the next plan may raise the budget to the
      limit (snappy: 2 gyroid phantoms went from 8 and 8.8 cells across to 12 and 11.8).
    The gate's own whole-part estimate (rebuild_cells) is not used: the ladder refines the narrow
    spots only, which costs far less than refining every wall cell."""
    plan = c.get("plan")
    if isinstance(plan, Mapping) and str(plan.get("status") or "") in HELD_PLANS:
        budget, limit = _num(plan.get("budget")), _num(plan.get("limit"))
        if budget is not None and limit is not None and limit > 0 and budget >= 0.999 * limit:
            return True
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
    of attempts. The time left never decides it (D3, docs/RULES.md TIME-1): the run's deadlines are
    guards only (TIME-5), and a rung that runs past them is a guard trip (TIME-3), after which the
    earlier sound mesh is still delivered (LAD-cfd-3). `attempt_seconds` and `now` are kept for
    callers and are not read."""
    c = resolution_caveat(state)
    if c is None or not switched(state):
        return False, "no switch"
    if current_rung(state) != 0 or ladder_of(state).get("after_switch"):
        return False, "this engine already climbed a rung"
    if not rung_left(state) or _over_limit(c):
        return False, "no rung fits"
    if int(state.get("retry_count", 0) or 0) >= int(attempts_ceiling):
        return False, "the run's ceiling of attempts is reached"
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


def resolution_of(entry: Mapping) -> float:
    """How well a sound mesh resolves its narrowest gap, for choosing between a run's sound meshes:
    infinite when it meets the floor (no resolution note), else its cells across (0 unmeasured)."""
    c = resolution_caveat(list(entry.get("requirement_caveats") or ()))
    if c is None:
        return math.inf
    return _num(c.get("cells_across")) or 0.0


def deliverable_sound(state: Mapping) -> dict | None:
    """The sound mesh the run delivers (LAD-cfd-3): of the run's sound meshes whose manifest still
    reads with no fatal defect, the one that resolves its narrowest gap best (resolution_of), the
    newest on a tie. A rung that came out coarser - snappy's re-plan can spend the budget on the
    background (lab gr-br2: 4 of 8 rungs) - never makes the delivery worse. None when there is
    none."""
    best: dict | None = None
    rows = [r for r in (state.get(SOUND_KEY) or ()) if isinstance(r, Mapping)]
    # CFDREQ-28: meshes of two engines are chosen by the product's own verdict (_beats)
    ctx = verdict_context(state) if len({str(r.get("engine") or "") for r in rows}) > 1 else None
    for row in rows:
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
        # LAD-cfd-8: a mesh a gate refused (kept deliverable, `refused`) ranks under every mesh no gate
        # refused, whatever its resolution - the best by the measured limits, never simply the last
        cand = {**dict(row), "manifest": manifest}
        if best is None or _beats(cand, best, ctx):
            best = cand
    return best


#: LAD-cfd-9: two refusals by the same gate this close (engine_fallback.gate_distance, in the gate's own
#: unit: log of the area ratio past the band, a share of the CAD, decades of residual) are as far off as
#: each other - the choice goes on to quality and the wall ask. abc 00001167: 0.438 and 0.447 of the inlet
#: are 0.021 apart past the 0.6 edge, noise of the staging, not a better mesh.
GATE_DISTANCE_TIE = 0.05
#: ...and two layer coverages this many points apart are the same answer to the wall ask.
COVERAGE_TIE_PCT = 5.0
#: Non-orthogonality a solver cannot take (the same bar as engine_fallback.SOLVER_HARD_NON_ORTHO).
_HARD_NON_ORTHO = 85.0


#: THE RULE (docs/RULES.md CFDREQ-28, amended in rulebook 112): when a run holds meshes from two engines - a gate
#: moved it on - it ends on the better of them by the product's own measured verdict, and a kept mesh is always
#: handed back when the next engine fails, is refused or runs out of time.
ENGINE_BETTER_OF_TWO = "CFDREQ-28"

#: CFDREQ-28: two meshes whose worst missed row is missed by shares of its limit this close are missed alike
#: (a hundredth of the limit: 0.8 of 80% coverage, 0.7 degrees of 70)
MISS_TIE = 0.01

#: The refusals whose measure is already a row of the verdict (verdict_limits): their concern is that row, not
#: a second, separate requirement missed - CFDREQ-28's layer_stack is the layer row (layer_coverage_pct, 80%)
GATES_THAT_ARE_ROWS = frozenset({"layer_stack"})


def near_misses(caveats) -> int:
    """The stated requirements a delivered mesh near-missed, as the verdict counts them (requirement_near_misses):
    every caveat but our own notes (NOTE_KINDS) and a refusal whose measure is already a verdict row
    (GATES_THAT_ARE_ROWS) - one shortfall is one missed row, never two."""
    return sum(1 for c in caveats or () if isinstance(c, Mapping) and c.get("kind") not in NOTE_KINDS
               and str(c.get("gate") or "") not in GATES_THAT_ARE_ROWS)


def _row_miss(row: Mapping) -> float:
    """How far a missed verdict row is past its limit, as a share of that limit (1.0 when it is not a number)."""
    m, lim = _num(row.get("measured")), _num(row.get("limit"))
    if m is None or lim is None:
        return 1.0
    scale = abs(lim) if lim else 1.0
    if str(row.get("op")) in (">=", ">"):
        return max(0.0, lim - m) / scale
    return max(0.0, m - lim) / scale


def verdict_standing(entry: Mapping, *, topology: str, layers_planned: bool) -> dict | None:
    """CFDREQ-28: one sound mesh of the run as the product's own measured verdict reads it (engines/verdict_limits
    and contracts/decision_record.verdict, the rows terminal assembly reads): {"outcome", "rank", "missed",
    "worst"} - rank 0 pass, 1 concerns, 2 anything else; `missed` the rows it misses (key, measured, limit); `worst`
    the largest of their misses as a share of the row's limit. None when its manifest cannot be read."""
    manifest = entry.get("manifest")
    if not isinstance(manifest, Mapping) or not isinstance(manifest.get("quality"), Mapping):
        return None
    try:
        from meshpipeline.contracts import decision_record as dr
        from meshpipeline.engines import verdict_limits as vl
        inputs = vl.inputs_for(engine=str(entry.get("engine") or ""), topology=topology, manifest=manifest,
                               valid=True, layers_planned=layers_planned,
                               requirement_near_misses=near_misses(entry.get("requirement_caveats")))
        outcome, _rule = dr.verdict(inputs)
        missed = [dict(r) for r in dr.missed_limits(inputs)]
    except Exception:  # noqa: BLE001 - a mesh the verdict cannot read is chosen as before
        logger.debug("resolution policy: verdict standing not read", exc_info=True)
        return None
    return {"outcome": outcome, "rank": {"pass": 0, "concerns": 1}.get(str(outcome), 2),
            "missed": [{k: r.get(k) for k in ("key", "label", "measured", "op", "limit")} for r in missed],
            "worst": max((_row_miss(r) for r in missed), default=0.0)}


def standing_order(a: Mapping, b: Mapping) -> int:
    """CFDREQ-28's plain order of two meshes by the measured verdict: +1 when `a` is the better, -1 when `b` is,
    0 when they tie. Pass beats concerns beats anything else; of two with concerns, the one missing fewer rows;
    then the one whose worst missed row is missed by less (a share of its own limit, MISS_TIE apart)."""
    if a["rank"] != b["rank"]:
        return 1 if a["rank"] < b["rank"] else -1
    if len(a["missed"]) != len(b["missed"]):
        return 1 if len(a["missed"]) < len(b["missed"]) else -1
    if abs(float(a["worst"]) - float(b["worst"])) > MISS_TIE:
        return 1 if a["worst"] < b["worst"] else -1
    return 0


def verdict_context(state: Mapping) -> dict:
    """What the verdict needs that is the run's, not one mesh's: the flow topology (the purpose's) and whether the
    approved plan builds layers (the same for every mesh of the run, so read off any of their records: a planned
    count, or a measured coverage)."""
    from meshpipeline.engines.purposes import topology_of
    try:
        topology = str(state.get("flow_topology") or topology_of(str(state.get("purpose") or "")) or "")
    except Exception:  # noqa: BLE001 - an unknown purpose reads as no topology
        topology = ""
    planned = False
    for row in [r for r in (state.get(SOUND_KEY) or ()) if isinstance(r, Mapping)]:
        q = _manifest_quality(str(row.get("workspace") or ""))
        if (_num(q.get("layers_requested")) or 0) >= 1 or _num(q.get("layer_coverage_pct")) is not None:
            planned = True
    return {"topology": topology, "layers_planned": planned}


def _manifest_quality(ws: str) -> Mapping:
    try:
        m = json.loads((Path(ws) / "mesh_manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    q = m.get("quality") if isinstance(m, Mapping) else None
    return q if isinstance(q, Mapping) else {}


def _by_verdict(a: Mapping, b: Mapping, ctx: Mapping | None) -> int | None:
    """CFDREQ-28 between meshes of TWO ENGINES: +1 / -1 by standing_order, and on a tie the first (the cheaper,
    it was built before the gate moved the run) is kept. None when both are one engine's, or either cannot be read -
    the order below decides, as it always did."""
    if not ctx or str(a.get("engine") or "") == str(b.get("engine") or ""):
        return None
    pa, pb = _past_hard(a), _past_hard(b)
    if pa != pb:
        # LAD-cfd-11 first: a mesh past the solver's hard non-orthogonality is the last resort, whatever it misses
        return -1 if pa else 1
    sa = a.get("standing") or verdict_standing(a, **ctx)
    sb = b.get("standing") or verdict_standing(b, **ctx)
    if not sa or not sb:
        return None
    got = standing_order(sa, sb)
    if got:
        return got
    if a.get("refused") and str(a.get("refused")) == str(b.get("refused")):
        # the same gate refused both and the verdict reads them alike: the one nearer that gate's bar (LAD-cfd-9)
        return None
    return 1 if int(a.get("attempt") or 0) < int(b.get("attempt") or 0) else -1


def engine_choice(state: Mapping) -> dict | None:
    """CFDREQ-28: when the run's sound meshes come from more than one engine, the one it ends on
    (deliverable_sound) and the best mesh of another engine it was chosen over, each with its standing, and the one
    plain line that says which was not chosen and why. None when every sound mesh is one engine's."""
    rows = [r for r in (state.get(SOUND_KEY) or ()) if isinstance(r, Mapping)]
    if len({str(r.get("engine") or "") for r in rows}) < 2:
        return None
    best = deliverable_sound(state)
    if best is None:
        return None
    ctx = verdict_context(state)
    others = []
    for row in rows:
        if str(row.get("engine") or "") == str(best.get("engine") or ""):
            continue
        try:
            manifest = json.loads((Path(str(row.get("workspace") or "")) / "mesh_manifest.json")
                                  .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(manifest, dict):
            continue
        cand = {**dict(row), "manifest": manifest}
        cand["standing"] = verdict_standing(cand, **ctx)
        others.append(cand)
    if not others:
        return None
    sb = verdict_standing(best, **ctx)
    other = others[0]
    for o in others[1:]:
        if o.get("standing") and other.get("standing") and standing_order(o["standing"], other["standing"]) > 0:
            other = o
    return {"best": best, "other": other, "rule": ENGINE_BETTER_OF_TWO,
            "line": choice_line(best, sb, other, other.get("standing"))}


def _missed_words(s: Mapping | None) -> str:
    if not s:
        return "its checks could not be read"
    rows = s.get("missed") or []
    if not rows:
        return "it passed every check"
    said = "; ".join(f"{r.get('label') or r.get('key')} {_g(r.get('measured'))} against {_g(r.get('limit'))}"
                     for r in rows[:3])
    return f"it missed {len(rows)} check{'s' if len(rows) != 1 else ''} ({said})"


def _g(v: Any) -> str:
    n = _num(v)
    return f"{n:.3g}" if n is not None else str(v)


def choice_line(best: Mapping, sb: Mapping | None, other: Mapping, so: Mapping | None) -> str:
    """The one plain line on the result: the mesh not chosen, and why (CFDREQ-28)."""
    from meshpipeline.engines.registry import engine_label
    tie = sb is not None and so is not None and standing_order(sb, so) == 0
    return (f"the {engine_label(str(other.get('engine') or ''))} mesh of attempt {other.get('attempt')} was not "
            f"chosen: {_missed_words(so)}, and this one "
            + ("measures the same and was built first" if tie else
               _missed_words(sb).replace("it ", "", 1)))


def _beats(a: Mapping, b: Mapping, ctx: Mapping | None = None) -> bool:
    """Whether sound mesh `a` (the newer) is delivered over `b`. MESHES OF TWO ENGINES go by the product's own
    measured verdict first, the first one on a tie (CFDREQ-28, _by_verdict). A mesh no gate refused beats a
    refused one; two unrefused ones go by resolution, the newer on a tie (LAD-cfd-3). TWO REFUSED ONES (LAD-cfd-9)
    never go by ladder order: when the same gate refused both, the one nearer its bar first; then resolution;
    then inside the solver's hard non-orthogonality; then the wall ask (layer coverage); then the lower
    worst non-orthogonality; only then the newer."""
    by = _by_verdict(a, b, ctx)
    if by is not None:
        return by > 0
    ra, rb = bool(a.get("refused")), bool(b.get("refused"))
    if ra != rb:
        return rb
    if not ra:
        return resolution_of(a) >= resolution_of(b)
    for cmp in _refused_order(a, b):
        if cmp:
            return cmp > 0
    return True


def _refused_order(a: Mapping, b: Mapping):
    """+1 where `a` is the better refused mesh on that count, -1 where `b` is, 0 where they tie. A mesh past
    the solver's hard non-orthogonality ranks under every mesh inside it, before anything else (LAD-cfd-11):
    it is kept only as the last resort."""
    pa, pb = _past_hard(a), _past_hard(b)
    yield 0 if pa == pb else (-1 if pa else 1)
    da, db = _num(a.get("refused_distance")), _num(b.get("refused_distance"))
    if (str(a.get("refused")) == str(b.get("refused")) and da is not None and db is not None
            and abs(da - db) > GATE_DISTANCE_TIE):
        yield 1 if da < db else -1
    res_a, res_b = resolution_of(a), resolution_of(b)
    yield 0 if res_a == res_b else (1 if res_a > res_b else -1)
    na, nb = _quality_num(a, "max_non_ortho"), _quality_num(b, "max_non_ortho")
    ia = na is not None and na <= _HARD_NON_ORTHO
    ib = nb is not None and nb <= _HARD_NON_ORTHO
    yield 0 if ia == ib else (1 if ia else -1)
    ca, cb = _quality_num(a, "layer_coverage_pct"), _quality_num(b, "layer_coverage_pct")
    if ca is not None and cb is not None and abs(ca - cb) > COVERAGE_TIE_PCT:
        yield 1 if ca > cb else -1
    if na is not None and nb is not None and na != nb:
        yield 1 if na < nb else -1


def _past_hard(entry: Mapping) -> bool:
    """The sound mesh's worst non-orthogonality is measured past the solver's hard bar (LAD-cfd-11)."""
    n = _quality_num(entry, "max_non_ortho")
    return n is not None and n > _HARD_NON_ORTHO


def _quality_num(entry: Mapping, key: str) -> float | None:
    manifest = entry.get("manifest")
    q = manifest.get("quality") if isinstance(manifest, Mapping) else None
    return _num(q.get(key)) if isinstance(q, Mapping) else None


def better_earlier_sound(state: Mapping) -> dict | None:
    """When the run's last attempt is sound and its ladder spent: an earlier sound mesh of the run
    that resolves the narrowest gap strictly better (deliverable_sound), else None."""
    rows = [r for r in (state.get(SOUND_KEY) or ()) if isinstance(r, Mapping)]
    if len(rows) < 2:
        return None
    best = deliverable_sound(state)
    if best is None:
        return None
    current = str(state.get("openfoam_workspace") or rows[-1].get("workspace") or "")
    if str(best.get("workspace") or "") == current:
        return None
    mine = next((r for r in rows if str(r.get("workspace") or "") == current), rows[-1])
    return best if resolution_of(best) > resolution_of(mine) else None


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
                    words=(f"the sizing rule needs about {rec.get('needed')} cells, over the "
                           f"{rec.get('limit')} this run may build: meshed as that holds (a count "
                           "the user states is meshed on a bigger machine, RUN-9)"))
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
        carried = _num(c.get("machine_cells"))
        if cells and limit and carried and carried > limit and cells <= carried:
            # RUN-9: meshed on a bigger machine sized for the count asked for - only the size is said
            return [f"  - The mesh has {cells / 1e6:.1f} M cells. Make sure your solver machine has the "
                    "memory for it."]
        # (never "the size your geometry needs": a count the user asked for makes a mesh this big
        # too, and is theirs - Areen, 14 Nov)
        return [f"  - The mesh has {cells / 1e6:.1f} M cells, more than the {limit / 1e6:.0f} M "
                "one standard job is sized for." if cells and limit else
                "  - The mesh has more cells than one standard job is sized for.",
                "    It is sound, so it is delivered as it is. Make sure your solver machine has "
                "the memory for it."]
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
    raw_plan = c.get("plan")
    plan: Mapping = raw_plan if isinstance(raw_plan, Mapping) else {}
    needed, limit = _num(plan.get("needed")), _num(plan.get("limit"))
    if plan.get("route_big") and needed and limit:
        # PLAN-cfd-3: the full rule's mesh is past one job; say so in plain numbers
        from meshpipeline.contracts.mesh_machine import normal_cells
        if int(limit) != normal_cells():
            # the plan was held to the user's own count (CFDREQ-3): theirs, not a limit of ours
            lines.append(f"    Our full rule for every gap would need about {needed / 1e6:.0f} M cells, "
                         f"more than the {limit / 1e6:.3g} M you asked for, so this mesh puts as many "
                         "cells in the gaps as that allows.")
        else:
            lines.append(f"    Our full rule for every gap would need about {needed / 1e6:.0f} M cells, "
                         f"more than the {limit / 1e6:.0f} M one job can build, so this mesh puts "
                         "as many cells in the gaps as that limit allows. Ask for about "
                         f"{needed / 1e6:.0f} M cells in your notes and it is meshed on a bigger machine.")
    lines.append("    The mesh is sound - no broken cells - so it is delivered. If the flow in "
                 "that gap matters to you, ask for a finer mesh there.")
    return lines


__all__ = ["CELL_LIMIT_PROVES", "ROOM_SHARE", "cite_plan", "ENGINE_BETTER_OF_TWO", "GATES_THAT_ARE_ROWS", "MISS_TIE",
           "choice_line", "engine_choice", "near_misses", "standing_order", "verdict_context", "verdict_standing",
           "gate_cell_limit", "LADDER_FILE", "LAD_AFTER_SWITCH", "LAD_CELL_LIMIT", "LAD_DELIVER_SOUND", "LAD_RUNG",
           "NOTE_GATES", "NOTE_KINDS", "OVER_CELL_LIMIT", "PLAN_GAP_LOCAL", "PLAN_PORTS",
           "PLAN_ROUTE_BIG", "PORT_CELLS_ACROSS", "RESOLUTION_SHORT", "RUNG_CAP", "RUNG_FACTORS",
           "HELD_PLANS", "SOUND_KEY", "STATE_KEY", "better_earlier_sound", "caveat", "current_rung", "deliverable_sound", "fact_for",
           "factor_of", "is_note_gate", "ladder_of", "next_ladder", "note_caveats", "note_lines",
           "read_fact", "resolution_caveat", "resolution_of", "retry_can_change", "rung_after_switch",
           "rung_factor", "rung_left", "sound_entry", "switched", "under_resolved_facts",
           "with_sound", "write_fact"]
