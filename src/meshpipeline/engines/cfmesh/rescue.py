# Responsibility: Give a cfMesh mesh that came out at or past its validity edge the engine's own second chances, inside the run it was built in, and keep whichever mesh measures best.
# Owns: the near-edge trigger, the order of the quality levers, the ranking of candidate meshes, and the rescue record.
# Boundaries: it runs cfMesh's own binaries on the workspace it is handed and measures with checkMesh; it never changes what was asked (cells, layers, patches) and never judges delivery - the gates do.
# Collaborates with: engines/cfmesh/native.py (calls it after the first checkMesh), foam_exec.check_mesh.
"""A CFMESH MESH AT ITS VALIDITY EDGE GETS THE ENGINE'S OWN SECOND CHANCES.

cartesianMesh is threaded and not deterministic. Near the edge of what it can mesh, the same
case comes out differently run to run: volute_scroll_001 gave a worst non-orthogonality of 82.4,
84.3 and 85.6 degrees on three identical runs, and once a face pointing the wrong way, which no
solver accepts (cfm-before / cfm-after / cfm-rerun, 2026-10-18). Its final untangling had left
ONE face. Delivered as it came, the run is a coin toss.

So a mesh that comes out broken (checkMesh's fatal defects) or past NEAR_EDGE_NON_ORTHO is not
handed over as it is. This module tries, in order (each step is PLANNED WORK - optimiser loops
planned from the mesh's cells, one re-mesh of the same cells - and runs whenever it is triggered and
its cell limit allows; the clock never skips one, docs/RULES.md TIME-1 and TIME-7):

  1. OPTIMISE: cfMesh's own improveMeshQuality on the mesh as built - the optimiser cartesianMesh
     runs last, run again with more passes. Same cells, same layers, points moved.
  2. RE-MESH, PROTECTING THE LAYERS: cartesianMesh again with the boundary-layer optimiser asked
     for more smoothing passes and a lower featureSizeFactor (layers may be no thicker than this
     share of the local feature size, so the stacks off two sides of a thin lip or a sharp
     trailing edge no longer meet), then step 1 on that mesh if it is still near the edge. Same
     cell sizes, same layer count; only how the layers are fitted changes.

THE CHEAPER LEVER GOES FIRST (TIME-19), from the planned work of each (remesh_first): the re-mesh is one
cfMesh run of the cells, the optimiser its planned loops over them, so on any mesh today the re-mesh runs
first and the optimiser only when the best so far is still near the edge. The optimiser then takes the
candidates best first, and never one the best so far already beats.

Every candidate is measured with checkMesh and the BEST MEASURED mesh is kept: one with no fatal
defect beats any with one, then the lowest worst non-orthogonality wins. The mesh as built is a
candidate too, so a lever can only ever improve what is delivered. What was tried and what was
kept is recorded beside the mesh (RESCUE_FILE) and in the quality record, so the run says it in
plain words; the losing meshes are deleted before the workspace goes home.

A mesh that is still broken after this is the engine's failing on the shape, and the fallback
ladder moves the run on to the next engine (pipeline/engine_fallback: a broken mesh moves on).

THE CLOCK IS A GUARD ONLY. Each step's guard is 3 times its planned time (TIME-2: its planned
work at the step's slow rate), never past what is left of the run's guard. A step its guard stops
is a guard trip, and the rescue stops there:
  - the mesh as built (or a better candidate already measured) is VALID - no fatal defect: it is
    kept, with a note, and the run goes on to its checks with it (TIME-8). A valid mesh is never
    thrown away for "ran out of time";
  - nothing valid is in hand: the attempt ends as an infrastructure failure and is replayed
    (TIME-3).
Before the loops were planned, the optimiser ran cfMesh's 50 loops whatever the mesh, and on a
tangled 2.5-4 M cell mesh that ran for over 3.5 hours (the full benchmark of 29 Oct: 4 files main
delivered came back "ran out of time").
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from meshpipeline.contracts.work_limits import (
    GUARD_FACTOR,
    TIME_KEEP_VALID,
    TIME_PASSAGE_STEP,
    TIME_REPAIR_WORK,
    TIME_TRIP,
    PlannedWork,
    guard_for,
)
from meshpipeline.engines.determinism import note_guard_trip
from meshpipeline.engines.verdict_limits import production_limit

logger = logging.getLogger(__name__)

#: LAD-cfd-15: a valid mesh whose worst face is more than this many degrees out of square is near the edge - the
#: limit the verdict itself holds a cfMesh mesh to (engines/verdict_limits PRODUCTION_LIMITS max_non_ortho, read from
#: there, never a copy). It was 75 before: a first mesh landing at 70-75 was delivered "with concerns" without the
#: re-mesh that, on abc 00164305 (e34, 25 Nov), brought the same cells to 60.8 every time it fired (first meshes of
#: that part read 70.4-92.8 run to run). The best measured mesh is still the one kept, the first mesh included.
NEAR_EDGE_NON_ORTHO = float(production_limit("max_non_ortho", "cfmesh"))
#: the rule a rescue's trigger is recorded under (the rescue record's `trigger_rule`)
NEAR_EDGE_RULE = "LAD-cfd-15"
#: Only a candidate this many degrees better than the best so far displaces it (checkMesh's
#: figure moves by a few hundredths between two reads of the same mesh).
MIN_GAIN_DEG = 0.5
#: The biggest mesh a rescue is tried on: each candidate is a whole extra polyMesh on the run's
#: disk - in memory on Cloud Run (16-32 GiB jobs), about 1.2 GB raw for 6.7 M cells - beside the
#: one kept. The mitral ventricle base came out invalid at 5.2 M cells once 5 layers were asked.
RESCUE_MAX_CELLS = 6_000_000
#: ...and the biggest one re-meshed: a second cartesianMesh holds its own working set on top.
#: Between the two, only the optimiser is tried.
REMESH_MAX_CELLS = 4_000_000
#: improveMeshQuality's sweeps per loop and its wall-point moves (cfMesh's defaults: 50 and 2; more
#: surface iterations let it move the wall points a fold sits on).
OPTIMISE_ITERATIONS = 50
OPTIMISE_SURFACE_ITERATIONS = 5
#: THE OPTIMISER'S PLANNED WORK (TIME-7). improveMeshQuality untangles in global loops (-nLoops):
#: each one smooths the bad region and the cells round it (up to OPTIMISE_ITERATIONS sweeps), then
#: moves the wall points, and the region it works on grows every other loop. The loops are PLANNED
#: from the mesh's cells: OPTIMISE_CELL_LOOPS shared over the cells, within OPTIMISE_LOOPS_MIN
#: (cfMesh's own default) and OPTIMISE_LOOPS_MAX - so a 2 M cell mesh gets 20 loops and a 4 M cell
#: one 10. The rescue asked 50 before, whatever the mesh.
#: Set on 8 dev-side cfMesh meshes (the held-out side never tunes), each re-meshed as built and
#: optimised on one 8-core production slot, 29 Oct:
#:   - 6 meshes untangle in 2 or 3 loops and the whole optimiser takes 50-140 s: the planned loops
#:     change nothing for them;
#:   - on the tangled ones (vmr_0050, vmr_0288) the bad faces stall at 3-19 from loop 2-5 and never
#:     clear, while each loop costs more (the region grows): 0288, 1.4 M cells, took 498 s for 20
#:     loops and 1,263 s for 29; 0050, 5.6 M cells, 164 s for 10, 1,103 s for 30 - and past 3.5
#:     hours for 50 on the full benchmark, where 4 files main delivered ran out of time.
OPTIMISE_LOOPS_MAX = 20
OPTIMISE_LOOPS_MIN = 10
OPTIMISE_CELL_LOOPS = 40_000_000
#: The optimiser's SLOW rate, in cell-loops a second. A loop's cost follows the tangled region, not
#: the cells: on the dev-side meshes above, 0288 averaged 56,000 over its 20 loops, while the first
#: loop of a heavily tangled one (vmr_0249, 288,000 bad faces) ran at 3,200. Its guard is
#: GUARD_FACTOR times the planned time at this rate, never past what is left of the run's guard.
OPTIMISE_RATE = 5_000.0
#: THE OPTIMISER STOPS WHEN IT IS NOT UNTANGLING (TIME-7, measured, never the clock). It prints its
#: bad faces as each untangling loop starts. On every dev-side mesh it untangled, or brought down to
#: a handful, the count at its 3rd loop was under 3% of the 1st (vmr_0249: 288,087, 2,070, 161;
#: vmr_0288: 7,184, 103, 169). One that is still at a tenth of it, and at least
#: UNTANGLE_STALL_MIN_FACES, is not untangling: the full benchmark's microfluidics spiral read
#: 52,518, 35,250, 52,544 at 6 minutes a loop. It is stopped there - its mesh is not kept - and the
#: rescue goes on with the best mesh measured so far.
UNTANGLE_CHECK_LOOP = 3
UNTANGLE_STALL_SHARE = 0.10
UNTANGLE_STALL_MIN_FACES = 1000
#: How improveMeshQuality says an untangling loop starts.
_BAD_FACES_AT_LOOP = re.compile(r"Iteration 0\. Number of bad faces is (\d+)")
#: The least guard a rescue step gets (the optimiser's old least budget, 300 s).
STEP_FLOOR_S = 300.0
#: The boundary-layer optimiser's settings for the protective re-mesh (cfMesh's defaults:
#: nSmoothNormals 5, maxNumIterations 5, featureSizeFactor 0.3, relThicknessTol 0.1). cfMesh
#: refuses a featureSizeFactor or relThicknessTol outside (0, 1).
PROTECTIVE_LAYER_OPTIMISATION = {"nSmoothNormals": 10, "maxNumIterations": 10,
                                 "featureSizeFactor": 0.15, "relThicknessTol": 0.05}
#: The record beside the mesh.
RESCUE_FILE = "mesh_rescue.json"
#: The scratch directory candidates live in while the rescue runs; never shipped.
SCRATCH = "_rescue"

_POLYMESH = Path("constant") / "polyMesh"
#: The name the protective re-mesh is recorded and kept under.
REMESHED = "re-meshed protecting the layers"
#: INS-30: a rescued mesh keeps every opening the mesh as built held, and a re-mesh carries the further passages
RESCUE_PORTS_RULE = "INS-30"
#: TIME-19: the cheaper lever first (planned work), and no optimiser on a candidate the best so far already beats
RESCUE_ORDER_RULE = "TIME-19"


def faced_patches(polymesh: Path) -> set[str] | None:
    """INS-30: the patches a mesh holds faces of, or None when its boundary cannot be read (nothing judged)."""
    from meshpipeline.engines.layer_census import NotMeasurable, _boundary
    try:
        return {name for name, (_, n) in _boundary(polymesh / "boundary").items() if int(n) > 0}
    except (OSError, NotMeasurable):
        return None


def untangle_stalled(log_text: str) -> str:
    """Why the optimiser whose log reads `log_text` is not untangling ('' while it may be): its bad
    faces at its UNTANGLE_CHECK_LOOP-th untangling loop are still at least UNTANGLE_STALL_SHARE of
    its first loop's, and at least UNTANGLE_STALL_MIN_FACES. Read from the run's own count, so the
    same mesh stops at the same loop on any machine."""
    counts = [int(m.group(1)) for m in _BAD_FACES_AT_LOOP.finditer(str(log_text or ""))]
    if len(counts) < UNTANGLE_CHECK_LOOP:
        return ""
    first, now = counts[0], counts[UNTANGLE_CHECK_LOOP - 1]
    if first > 0 and now >= UNTANGLE_STALL_MIN_FACES and now >= UNTANGLE_STALL_SHARE * first:
        return (f"it was not untangling: {first:,} bad faces at its first loop, {now:,} at loop "
                f"{UNTANGLE_CHECK_LOOP}")
    return ""


def optimise_loops(cells: float | None) -> int:
    """The optimiser's planned loops for a mesh of `cells` cells (TIME-7): OPTIMISE_CELL_LOOPS
    shared over the cells, within OPTIMISE_LOOPS_MIN..OPTIMISE_LOOPS_MAX. The same cells give the
    same loops on any machine."""
    n = _num(cells) or 0.0
    if n <= 0:
        return OPTIMISE_LOOPS_MIN
    return int(max(OPTIMISE_LOOPS_MIN, min(OPTIMISE_LOOPS_MAX, OPTIMISE_CELL_LOOPS // n)))


def optimise_args(cells: float | None) -> str:
    """improveMeshQuality's arguments for a mesh of `cells` cells."""
    return (f"-nLoops {optimise_loops(cells)} -nIterations {OPTIMISE_ITERATIONS} "
            f"-nSurfaceIterations {OPTIMISE_SURFACE_ITERATIONS}")


def optimise_work(cells: float | None, *, ceiling_s: float) -> PlannedWork:
    """The optimiser's planned work (cells x its planned loops) and the guard that follows from it
    (TIME-2), never past `ceiling_s` - what is left of the run's guard."""
    n = _num(cells) or 0.0
    floor = min(STEP_FLOOR_S, max(1.0, float(ceiling_s)))
    return PlannedWork(where="cfmesh.rescue.optimise", unit="cell-loops",
                       amount=(n * optimise_loops(n) if n > 0 else None), rate=OPTIMISE_RATE,
                       floor_s=floor, ceiling_s=max(floor, float(ceiling_s)))


def remesh_work(cells: float | None, *, ceiling_s: float) -> PlannedWork:
    """The protective re-mesh's planned work: one cfMesh run of the same cells, guarded like the
    first one (contracts/work_limits.guard_for), never past `ceiling_s`."""
    floor = min(STEP_FLOOR_S, max(1.0, float(ceiling_s)))
    work = guard_for("cfmesh", cells=(_num(cells) or None), floor_s=floor,
                     ceiling_s=max(floor, float(ceiling_s)))
    return PlannedWork(where="cfmesh.rescue.remesh", unit=work.unit, amount=work.amount,
                       rate=work.rate, floor_s=work.floor_s, ceiling_s=work.ceiling_s)


def remesh_first(cells: float | None) -> bool:
    """Whether the protective re-mesh runs before the optimiser (TIME-19): the lever with less PLANNED work at its
    slow rate goes first - one cfMesh run of the cells (contracts/work_limits FILL_RATES) against the optimiser's
    planned loops over them (OPTIMISE_RATE). The same cells give the same order on any machine; the clock never
    decides it. abc 99768 (227,444 cells): the optimiser ran 2,063 s, the protective re-mesh 33 s."""
    r = remesh_work(cells, ceiling_s=1e9).planned_seconds
    o = optimise_work(cells, ceiling_s=1e9).planned_seconds
    return r is not None and o is not None and r < o


#: TIME-15: the CAD check runs on the mesh as built before a rescue whose planned work (re-mesh + optimiser, at their
#: slow rates) is at least this - a step's least guard; the check itself is one foamToVTK and one surface measure
FIDELITY_FIRST_MIN_S = STEP_FLOOR_S
#: TIME-15: a mesh as built that misses more than this share of the CAD surface the flow reaches is beyond any rescue:
#: LAD-cfd-8's "not the part" bar (pipeline/engine_fallback.NOT_THE_PART_SHARE), past which no refused mesh is ever
#: delivered. The fidelity gate refuses past 8% (cad_fidelity.CAD_MISSED_REBUILD); a rescue would have to bring back
#: over 42% of the CAD surface with the same cells, and it only moves points within a cell or re-fits layers
FIDELITY_FIRST_MISSED_SHARE = 0.5
FIDELITY_FIRST_RULE = "TIME-15"


def planned_rescue_seconds(cells: float | None) -> float | None:
    """The rescue's planned work in seconds at its slow rates (TIME-15): the protective re-mesh and the optimiser."""
    parts = [w.planned_seconds for w in (remesh_work(cells, ceiling_s=1e9), optimise_work(cells, ceiling_s=1e9))]
    known = [float(p) for p in parts if p is not None]
    return sum(known) if known else None


def beyond_rescue(fid: dict | None) -> dict | None:
    """TIME-15: {"missed_share", "words"} when the CAD check's record `fid` of the mesh as built misses more of the
    CAD surface than FIDELITY_FIRST_MISSED_SHARE; None otherwise (unmeasured, or within reach)."""
    if not isinstance(fid, dict):
        return None
    c2m = (fid.get("surface") or {}).get("cad_to_mesh") if isinstance(fid.get("surface"), dict) else None
    missed = _num((c2m or {}).get("cad_missed_share")) if isinstance(c2m, dict) else None
    if missed is None or missed <= FIDELITY_FIRST_MISSED_SHARE:
        return None
    from meshpipeline.engines.cad_fidelity import concerns
    words = " ".join(concerns(fid)) or f"About {missed * 100:.1f}% of your CAD surface is not reached by the mesh."
    return {"missed_share": round(missed, 4), "words": words}


def fidelity_first_refusal(first: dict):
    """TIME-15: the refusal an attempt ends with when its mesh as built was beyond any rescue - the CAD check's own
    gate and words (engines/cad_fidelity.gate_cad_fidelity), standing over the mesh on disk (pipeline/executor
    refusal_stands_over_mesh). No retry or other engine changes it (LAD-cfd-8: nothing bears on cad_fidelity)."""
    from meshpipeline.contracts.failure_cause import FailureCause
    from meshpipeline.engines.preflight import PreflightRefusal
    detail = f"the mesh does not follow your CAD: {first.get('words') or ''}".strip()
    return PreflightRefusal(
        gate="cad_fidelity", cause=FailureCause.PATCH_NOT_CAPTURED.value,
        builder_text=(f"[FIDELITY] {detail} The mesh was checked as built, before the rescue: a rescue moves points "
                      "on the same cells and cannot bring that surface back."),
        facts={"fidelity_first": True, "after_build": True, "deterministic": True, "rule": FIDELITY_FIRST_RULE,
               "detail": detail, "fidelity": first.get("fidelity"), "missed_share": first.get("missed_share"),
               "meshers_started": 1})


def _fidelity_first(ws: Path, cells: float, fidelity: Callable[[Path], dict | None] | None) -> dict | None:
    if fidelity is None:
        return None
    planned = planned_rescue_seconds(cells)
    if planned is None or planned < FIDELITY_FIRST_MIN_S:
        return None
    try:
        fid = fidelity(ws)
    except Exception:  # noqa: BLE001 - a check that could not run never skips the rescue
        logger.warning("cfMesh rescue: %s - the CAD check on the mesh as built could not run", FIDELITY_FIRST_RULE,
                       exc_info=True)
        return None
    gone = beyond_rescue(fid)
    if gone is None:
        return None
    gone.update(planned_s=round(planned, 1), fidelity={k: (fid or {}).get(k) for k in ("verdicts", "surface",
                                                                                         "volume")},
                skipped=(f"the mesh as built misses {gone['missed_share'] * 100:.1f}% of the CAD surface, more than "
                         f"{FIDELITY_FIRST_MISSED_SHARE * 100:.0f}%, and a rescue moves points on the same cells - it "
                         f"cannot bring that surface back ({FIDELITY_FIRST_RULE}; about {planned / 60:.0f} min of "
                         "planned rescue not run)"))
    return gone


def near_edge(q: dict | None, limit: float | None = None) -> str:
    """Why this mesh is at its validity edge, in words ('' when it is not). `limit`: a non-orthogonality the user
    asked for under ours (CFDREQ-5, engines/cfd_requirements.non_ortho_limit) - the engine's own levers are tried
    for it too; None: ours alone."""
    q = q or {}
    fatal = list(q.get("fatal") or [])
    if fatal:
        return "checkMesh found " + ", ".join(str(f) for f in fatal)
    no = _num(q.get("max_non_ortho"))
    edge = NEAR_EDGE_NON_ORTHO if limit is None else min(NEAR_EDGE_NON_ORTHO, float(limit))
    if no is not None and no > edge:
        return (f"its worst cell is {no:.1f} degrees out of square (over "
                f"{edge:.0f}{'' if limit is None or edge == NEAR_EDGE_NON_ORTHO else ', the limit you asked for'})")
    return ""


#: The worst non-orthogonality a CFD solver can correct (engine_fallback.SOLVER_HARD_NON_ORTHO): a candidate past
#: it ranks under every one inside it, whatever its layers.
HARD_NON_ORTHO = 85.0


def rank(q: dict | None) -> tuple:
    """The order candidates are kept in, lower first: checkMesh-valid (the fewest fatal defects, none beats any);
    inside the solver's hard limit (HARD_NON_ORTHO); the wall's layers kept at the planned height (CFDREQ-14: not
    `layers_lost`); then the lowest worst non-orthogonality, then the fewest highly skewed faces. The cfMesh resolve
    parts (CFD board, 22 Nov): kept as built for their layers they read 84.7-88.7 degrees, where the optimised
    candidates read 45-71 with no layer at the planned height - neither has both, and a mesh past 85 is not one a
    solver takes."""
    q = q or {}
    no = _num(q.get("max_non_ortho"))
    over = 1 if no is None or no > HARD_NON_ORTHO else 0
    return (len(q.get("fatal") or []), over, 1 if q.get("layers_lost") else 0, 180.0 if no is None else no,
            int(_num(q.get("skew_faces")) or 0))


#: CFDREQ-14: a candidate whose wall has its layers at the planned height on this many points less of the wall
#: than the mesh as built has lost them
LAYER_LOSS_PTS = 10.0


def layers_at_plan(workspace) -> float | None:
    """The share of the wall (percent) whose first cell stands at the first layer height the meshDict asked
    (engines/layer_census, CFDREQ-14), on the polyMesh in `workspace`; None with no planned height or no
    measure. cfMesh's optimiser smooths a 0.02 mm first layer into the cells round it: the rectangular duct
    reduction (resolved wall, 0.0246 mm asked) read a first cell of 0.020 mm as built and 1.72 mm optimised
    (median), its non-orthogonality going from 85.2 to 52.6 degrees - the layers traded for the angle."""
    from meshpipeline.engines.ground_plane import is_ground
    from meshpipeline.engines.layer_census import census, planned_first_of, wall_summary
    from meshpipeline.engines.workspace_facts import contract_patches
    ws = Path(workspace)
    planned = planned_first_of(ws)
    if not planned:
        return None
    walls = [str(c.get("name")) for c in contract_patches(ws) if str(c.get("type") or "") == "wall"
             and c.get("name") and not is_ground(str(c.get("name")), "wall")]
    if not walls:
        return None
    try:
        per, _why = census(ws / _POLYMESH, 1, walls, planned_first_m=planned)
    except Exception:  # noqa: BLE001 - evidence, not a verdict
        logger.warning("rescue: the layer census failed", exc_info=True)
        return None
    got = wall_summary(per).get("area_at_plan_pct") if per else None
    return float(got) if got is not None else None


def measured(q: dict | None) -> bool:
    """Whether `q` is a COMPLETE checkMesh reading: a cell count and a worst angle. checkMesh
    that died early still comes back as a record with no fatal defect listed - and an absent
    defect is no proof the mesh has none."""
    q = q or {}
    cells, no = _num(q.get("cells")), _num(q.get("max_non_ortho"))
    return bool(cells and cells > 0 and no is not None)


def better(cand: dict | None, best: dict | None) -> bool:
    """Whether `cand` measures better than `best` by a margin worth a swap. A candidate without a
    complete reading never wins."""
    if not measured(cand):
        return False
    a, b = rank(cand), rank(best)
    for i in (0, 1, 2):
        if a[i] != b[i]:
            return a[i] < b[i]
    return a[3] < b[3] - MIN_GAIN_DEG


def protective_meshdict(text: str) -> str | None:
    """The meshDict with the boundary-layer optimiser set to protect the layers
    (PROTECTIVE_LAYER_OPTIMISATION) and layer untangling on; None when the dict has no
    boundaryLayers block (nothing to protect). Any optimisationParameters already there are
    replaced, everything else is kept byte for byte."""
    m = re.search(r"^boundaryLayers\s*\n\{", text, flags=re.M)
    if not m:
        return None
    start = m.end()
    depth, i = 1, start
    while i < len(text) and depth:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    if depth:
        return None
    body = text[start:i - 1]
    body = re.sub(r"\n\s*optimisationParameters\s*\{[^{}]*\}", "", body)
    params = " ".join(f"{k} {v:g};" if isinstance(v, float) else f"{k} {v};"
                      for k, v in PROTECTIVE_LAYER_OPTIMISATION.items())
    extra = f"    optimisationParameters {{ {params} }}\n"
    if not re.search(r"^\s*untangleLayers\s", body, flags=re.M):
        extra += "    untangleLayers 1;\n"
    if not re.search(r"^\s*optimiseLayer\s", body, flags=re.M):
        extra += "    optimiseLayer 1;\n"
    body = body.rstrip() + "\n" + extra
    return text[:start] + body + text[i - 1:]


def rescue(workspace, q: dict, *, bashrc: str, deadline: float, mesher_seconds: float,
           check_seconds: float = 60.0,
           measure: Callable[..., dict] | None = None,
           runner: Callable[..., Any] | None = None, env: dict | None = None,
           is_2d: bool = False, limit: float | None = None,
           layers: Callable[..., float | None] | None = None,
           passages: Callable[..., tuple[int, bool, str]] | None = None,
           step_only: bool = False,
           fidelity: Callable[[Path], dict | None] | None = None) -> dict:
    """The quality record of the mesh left in `workspace` after the rescue: `q` itself when the
    mesh is not near its edge (or it is past a rescue's cell limit), else the best measured
    candidate's record, with `rescue` saying what was tried and kept. Each step runs its PLANNED
    work (TIME-7) under the guard that work gives it, never past `deadline` - the monotonic end of
    the run's GUARD. A step its guard stops is a guard trip (`guard_tripped` in the record), never
    a skip: with a valid mesh in hand that mesh is kept (`guard_kept`, TIME-8), else the attempt
    is replayed (TIME-3). `mesher_seconds` is the first cartesianMesh's wall time, kept in the
    record only; `check_seconds` the first checkMesh's - the room a step leaves for the
    measurement after it. Neither decides a step. INS-30: a candidate that lost an opening the mesh as built
    held is no candidate, and a protective re-mesh of a case with further passages (PASSAGES_FILE) has them
    meshed and merged again (`passages`, engines/cfmesh/native.mesh_passages) before it is measured - the re-mesh
    reads the case's own surface, the first passage only (abc 99768: outlet_3 lost in every rescued mesh).
    `step_only` (TIME-14): the rescue of ONE passage before it is merged (INS-31) - a trip with nothing valid in hand
    stops this rescue only, the passage goes on as built and the attempt is NOT ended (`guard_step_only`).
    `fidelity` (TIME-15): the CAD check's measure of the mesh as it stands (engines/cad_fidelity.measure), asked only
    before a rescue whose planned work is at least FIDELITY_FIRST_MIN_S; a mesh that misses more of the CAD than
    FIDELITY_FIRST_MISSED_SHARE is not rescued (`fidelity_first` in the record)."""
    why = near_edge(q, limit)
    if not why:
        return q
    ws = Path(workspace)
    record: dict = {"trigger": why, "trigger_rule": NEAR_EDGE_RULE, "before": _brief(q), "tried": [],
                    "kept": "as built"}
    cells = _num(q.get("cells")) or 0
    if is_2d:
        return _done(ws, q, {**record, "skipped": "2D case (cartesian2DMesh) - not rescued"})
    if cells > RESCUE_MAX_CELLS:
        return _done(ws, q, {**record, "skipped": (
            f"{int(cells):,} cells is over the {RESCUE_MAX_CELLS:,} a rescue may copy")})
    if not (ws / _POLYMESH / "owner").exists():
        return q
    # TIME-15: THE CAD CHECK FIRST, before a long rescue. A rescue moves points and re-fits layers on the same cells;
    # it never brings back CAD surface the mesh as built does not reach
    gone = _fidelity_first(ws, cells, fidelity)
    if gone:
        return _done(ws, q, {**record, "skipped": gone["skipped"], "rule": FIDELITY_FIRST_RULE,
                             "fidelity_first": gone})
    held = faced_patches(ws / _POLYMESH) or set()

    def _lost() -> list[str]:
        now = faced_patches(ws / _POLYMESH)
        return sorted(held - now) if now is not None else []

    from meshpipeline.cad.fluid_passages import PASSAGES_FILE
    from meshpipeline.engines.cfmesh.foam_exec import _foam_env, check_mesh
    from meshpipeline.sandbox.safe_exec import run_guarded
    check: Callable[..., dict] = measure if measure is not None else check_mesh
    run: Callable[..., Any] = runner if runner is not None else run_guarded
    if env is None:
        env = _foam_env()

    meshdict = ws / "system" / "meshDict"
    try:
        asked_dict = meshdict.read_text() if meshdict.exists() else ""
    except OSError:
        return q                       # nothing is moved before the case can be read back
    kept_dict = asked_dict
    # CFDREQ-14: the wall's layers at the planned height, as built - a candidate that loses them ranks under one
    # that keeps them (validity still first)
    count_layers: Callable[..., float | None] = layers if layers is not None else layers_at_plan
    base_layers = count_layers(ws)
    if base_layers is not None:
        q = {**q, "layer_area_at_plan_pct": base_layers}
        record["before"] = _brief(q)
    scratch = ws / SCRATCH
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    best_q, best_dir, best_name = q, scratch / "as_built", "as built"
    #: what one measurement of a candidate may take: a checkMesh like the first, with room
    check_s = 1.5 * max(float(check_seconds or 0.0), 10.0) + 15.0
    logger.warning("cfMesh mesh at its validity edge (%s) - trying the engine's own levers", why)

    def _left() -> float:
        """What is left of the run's guard, less the check that follows a step: the most any
        step's own guard may be (a guard, never a reason to skip the step)."""
        return max(1.0, deadline - time.monotonic() - check_s)

    def _guard(work: PlannedWork) -> float:
        """The subprocess timeout a step runs under: its own guard (TIME-2), within the run's."""
        return float(min(work.guard_seconds, _left()))

    def _trip(name: str, budget: float, work: PlannedWork) -> None:
        # THE GUARD STOPPED A STEP: recorded, and the rescue stops here. A valid mesh already in
        # hand - the mesh as built, or a better candidate measured - is kept, with a note, and the
        # run goes on to its checks (TIME-8). With nothing valid, the attempt is replayed as an
        # infrastructure failure (TIME-3). No candidate is judged on what the stopped step left.
        keep = measured(best_q) and not best_q.get("fatal")
        record["guard_tripped"] = name
        if keep:
            record["guard_kept"] = best_name
        elif step_only:
            # TIME-14: a passage's own rescue - the passage goes on as built, the attempt goes on to its own mesh
            record["guard_step_only"] = True
        planned = work.planned_seconds
        record["tried"].append({"step": name, "guard_tripped": True, "guard_s": round(budget, 1),
                                "planned_s": (round(planned, 1) if planned else None)})
        # TIME-13: stopped by ITS OWN guard (not by what was left of the run's), the step does the same planned
        # work on the same mesh when the attempt is run again - abc 99768's passage optimiser tripped at 316 s,
        # was run again unchanged and tripped at 316 s again - so the trip says so and no replay follows
        own = work.guard_seconds is not None and float(budget) >= float(work.guard_seconds) - 1.0
        rule = TIME_KEEP_VALID if keep else (TIME_PASSAGE_STEP if step_only else TIME_TRIP)
        note_guard_trip("cfmesh.rescue", rule=rule, step=name,
                        guard_s=float(budget), unit=work.unit, amount=work.amount,
                        planned_s=planned, kept=(best_name if keep else None),
                        same_on_replay=(True if own else None))

    def _measure(name: str) -> dict | None:
        (ws / "mesh_quality.json").unlink(missing_ok=True)     # never a cached figure
        try:
            got = check(ws, bashrc=bashrc) or None
        except Exception:  # noqa: BLE001 - an unmeasured candidate is no candidate
            logger.warning("rescue: checkMesh of the %s mesh failed", name, exc_info=True)
            return None
        if got and base_layers is not None:
            now = count_layers(ws)
            got = {**got, "layer_area_at_plan_pct": now,
                   "layers_lost": now is None or now < base_layers - LAYER_LOSS_PTS}
        return got

    def _consider(name: str, cand_q: dict | None, step: dict, built_by: str) -> None:
        nonlocal best_q, best_dir, best_name, kept_dict
        step.update(_brief(cand_q) if cand_q else {"measured": False})
        lost = _lost() if cand_q else []
        if lost:
            step.update(lost_openings=lost, openings_rule=RESCUE_PORTS_RULE)
        record["tried"].append(step)
        if cand_q and not lost and better(cand_q, best_q):
            new_dir = scratch / name.replace(" ", "_")
            shutil.rmtree(new_dir, ignore_errors=True)
            (ws / _POLYMESH).rename(new_dir)
            shutil.rmtree(best_dir, ignore_errors=True)
            best_q, best_dir, best_name = cand_q, new_dir, name
            kept_dict = built_by
        else:
            shutil.rmtree(ws / _POLYMESH, ignore_errors=True)

    def _optimise(src_dir: Path, name: str, built_by: str) -> None:
        if record.get("guard_tripped"):
            return
        # PLANNED WORK (TIME-7): loops planned from the cells, run to the end; the guard only
        # guards
        work = optimise_work(cells, ceiling_s=_left())
        budget = _guard(work)
        shutil.copytree(src_dir, ws / _POLYMESH)
        t = time.monotonic()
        log = ws / "improveMeshQuality.log"
        stalled: list[str] = []

        def _watch() -> str:
            # read from the run's own count, never the clock (TIME-7)
            if not stalled:
                try:
                    why = untangle_stalled(log.read_text(errors="replace"))
                except OSError:
                    why = ""
                if why:
                    stalled.append(why)
            return stalled[0] if stalled else ""

        rc = _run(run, ws, bashrc, env, f"improveMeshQuality {optimise_args(cells)}",
                  log.name, budget, watch=_watch)
        if rc == -1:
            shutil.rmtree(ws / _POLYMESH, ignore_errors=True)
            _trip(name, budget, work)
            return
        if rc == RC_STOPPED:
            # NOT UNTANGLING (TIME-7): stopped on its own count; what it left is not a candidate
            shutil.rmtree(ws / _POLYMESH, ignore_errors=True)
            record["tried"].append({"step": name, "stopped": stalled[0] if stalled else "",
                                    "seconds": round(time.monotonic() - t, 1),
                                    "loops": optimise_loops(cells), "rule": TIME_REPAIR_WORK})
            return
        step = {"step": name, "rc": rc, "seconds": round(time.monotonic() - t, 1),
                "loops": optimise_loops(cells), "rule": TIME_REPAIR_WORK}
        if rc != 0 or not (ws / _POLYMESH / "owner").exists():
            step["error"] = _tail(ws / "improveMeshQuality.log")
            record["tried"].append(step)
            shutil.rmtree(ws / _POLYMESH, ignore_errors=True)
            return
        _consider(name, _measure(name), step, built_by)

    def _remesh(protective: str) -> tuple[Path, dict] | None:
        """THE PROTECTIVE RE-MESH: cartesianMesh again on the protective dict (INS-30: a case of several passages has
        them meshed and merged in again). A re-mesh off the edge is considered at once; one still near it is kept
        aside and returned, a candidate for the optimiser. The dict as asked is back in place after it."""
        nonlocal best_q, best_dir, best_name, kept_dict
        meshdict.write_text(protective)
        try:
            t = time.monotonic()
            work = remesh_work(cells, ceiling_s=_left())
            budget = _guard(work)
            rc = _run(run, ws, bashrc, env, "cartesianMesh", "cartesianMesh_rescue.log", budget)
            step = {"step": REMESHED, "rc": rc, "seconds": round(time.monotonic() - t, 1)}
            if rc == 0 and (ws / _POLYMESH / "owner").exists() and (ws / PASSAGES_FILE).exists():
                # INS-30: the re-mesh is of the first passage; the others are meshed and merged into it again
                from meshpipeline.engines.cfmesh.native import mesh_passages
                merge: Callable[..., tuple[int, bool, str]] = passages if passages is not None else mesh_passages
                t_p = time.monotonic()
                p_rc, _, p_why = merge(ws, bashrc=bashrc, timeout=_left())
                step["passages_s"] = round(time.monotonic() - t_p, 1)
                if p_rc != 0:
                    rc, step["rc"] = p_rc if p_rc != -1 else 1, p_rc
                    step.update(passages=p_why or "the further passages did not mesh",
                                openings_rule=RESCUE_PORTS_RULE)
            if rc == -1:
                shutil.rmtree(ws / _POLYMESH, ignore_errors=True)
                _trip(REMESHED, budget, work)
                return None
            if rc != 0 or not (ws / _POLYMESH / "owner").exists():
                if "passages" not in step:
                    step["error"] = _tail(ws / "cartesianMesh_rescue.log")
                record["tried"].append(step)
                shutil.rmtree(ws / _POLYMESH, ignore_errors=True)
                return None
            remesh_q = _measure("re-meshed")
            lost = _lost() if remesh_q else []
            if lost:
                # INS-30: a re-mesh that lost an opening is no candidate, however square its cells
                record["tried"].append({**step, **_brief(remesh_q), "lost_openings": lost,
                                        "openings_rule": RESCUE_PORTS_RULE})
                shutil.rmtree(ws / _POLYMESH, ignore_errors=True)
                return None
            if remesh_q and near_edge(remesh_q, limit):
                # kept aside: the optimiser may take it next, and every candidate competes
                remesh_dir = scratch / "remeshed_raw"
                shutil.rmtree(remesh_dir, ignore_errors=True)
                (ws / _POLYMESH).rename(remesh_dir)
                record["tried"].append({**step, **_brief(remesh_q)})
                if better(remesh_q, best_q):
                    if best_dir != scratch / "as_built":     # the mesh as built stays for the optimiser's record
                        shutil.rmtree(best_dir, ignore_errors=True)
                    best_q, best_dir, best_name = remesh_q, remesh_dir, REMESHED
                    kept_dict = protective
                return remesh_dir, remesh_q
            _consider(REMESHED, remesh_q, step, protective)
            return None
        finally:
            meshdict.write_text(asked_dict)

    def _wants_more() -> bool:
        """Whether the best so far still wants a lever: near the edge, or it bought its angle with the wall's planned
        layers (CFDREQ-14) - the re-mesh that protects them may have both."""
        return bool((near_edge(best_q, limit) or best_q.get("layers_lost")) and not record.get("guard_tripped"))

    moved = False
    try:
        # the mesh as built is the first candidate, kept aside while the others are made
        as_built_dir = best_dir
        (ws / _POLYMESH).rename(as_built_dir)
        moved = True
        protective = protective_meshdict(asked_dict) if asked_dict else None
        can_remesh = protective is not None and cells <= REMESH_MAX_CELLS
        # TIME-19: the cheaper lever runs first, decided from the planned work of each (one cfMesh run of the cells,
        # or the optimiser's planned loops over them), never the clock
        first = can_remesh and remesh_first(cells)
        record["order"] = ["re-mesh", "optimise"] if first else ["optimise", "re-mesh"]
        if not first:
            # 1. the engine's optimiser on the mesh as built
            _optimise(as_built_dir, "optimised", asked_dict)
        raw: tuple[Path, dict] | None = None
        # 2. a protective re-mesh, when the best is still near the edge (its cell limit decides, never the time left)
        if protective is not None and _wants_more():
            if not can_remesh:
                record["tried"].append({"step": REMESHED, "skipped": (
                    f"{int(cells):,} cells is over the {REMESH_MAX_CELLS:,} a second run may "
                    "hold beside the first")})
            else:
                raw = _remesh(protective)
        # 3. the optimiser on the candidates still near the edge, the best first. TIME-19: a candidate the best so far
        #    already beats is not optimised - abc 99768's re-mesh (171 degrees, negative volumes) ran the optimiser
        #    2,729 s into its guard beside an optimised mesh of 87 degrees with no fatal defect
        pool: list[tuple[Path, dict, str, str]] = []
        if first and as_built_dir.exists():
            pool.append((as_built_dir, q, "optimised", asked_dict))
        if raw is not None and raw[0].exists():
            pool.append((raw[0], raw[1], REMESHED + ", then optimised", protective or asked_dict))
        pool.sort(key=lambda c: rank(c[1]))
        for src, src_q, name, built_by in pool:
            if not _wants_more():
                break
            if src != best_dir and better(best_q, src_q):
                record["tried"].append({"step": name, "rule": RESCUE_ORDER_RULE, "skipped": (
                    f"the mesh it starts from measures worse than the best so far ({best_name})")})
                continue
            _optimise(src, name, built_by)
        if raw is not None and best_dir != raw[0]:
            shutil.rmtree(raw[0], ignore_errors=True)
    except Exception:  # noqa: BLE001 - a lever must never lose the mesh already built
        logger.warning("cfMesh rescue failed part-way - keeping the best mesh so far",
                       exc_info=True)
    finally:
        if moved:
            shutil.rmtree(ws / _POLYMESH, ignore_errors=True)
            best_dir.rename(ws / _POLYMESH)
            if meshdict.exists() or kept_dict:
                # the meshDict beside the mesh is the one that built it
                meshdict.write_text(kept_dict)
        shutil.rmtree(scratch, ignore_errors=True)
    record["kept"] = best_name
    return _done(ws, best_q, record)


def _done(ws: Path, q: dict, record: dict) -> dict:
    record["after"] = _brief(q)
    record["note"] = note(record)
    try:
        (ws / RESCUE_FILE).write_text(json.dumps(record, default=str))
    except OSError:
        pass
    logger.warning("cfMesh rescue: %s", record["note"])
    return {**q, "rescue": record}


def note(record: dict) -> str:
    """The rescue in the engineer's words."""
    before, after = record.get("before") or {}, record.get("after") or {}
    head = f"The first cfMesh mesh was at its validity edge: {record.get('trigger')}."
    if record.get("skipped"):
        return f"{head} No second try: {record['skipped']}."
    if record.get("guard_tripped") and record.get("guard_kept"):
        return (f"{head} The '{record['guard_tripped']}' step ran past its guard clock "
                f"({GUARD_FACTOR:g} times its planned work), so it was stopped; the "
                f"{record['guard_kept']} mesh, measured valid before it, is kept: "
                f"{_words(after)}.")
    if record.get("guard_tripped") and record.get("guard_step_only"):
        return (f"{head} The '{record['guard_tripped']}' step ran past its guard clock ({GUARD_FACTOR:g} times its "
                f"planned work), so it was stopped before it finished; this part goes on as built ({_words(after)}) "
                "and the finished mesh is checked like any other.")
    if record.get("guard_tripped"):
        return (f"{head} The '{record['guard_tripped']}' step ran past its guard clock - an "
                "infrastructure stop, not a result: this attempt is run again as it was.")
    tried = [s["step"] for s in record.get("tried") or [] if not s.get("skipped")]
    short = wall_short(record)
    if short:
        head = f"{head} {short}"
    if record.get("kept") == "as built":
        lost = [s["step"] for s in record.get("tried") or [] if s.get("layers_lost") and not s.get("fatal")]
        why = (f" ({' and '.join(lost)} took the wall's layers away from the planned height)" if lost else "")
        return (f"{head} Tried {', then '.join(tried) or 'nothing'}; none measured "
                f"better{why}, so the mesh as built is kept.")
    return (f"{head} Kept the {record.get('kept')} mesh: "
            f"{_words(before)} before, {_words(after)} after.")


def wall_short(record: dict) -> str:
    """The plain words when no candidate had both - inside the hard limit AND the wall's layers at the planned
    height - and the kept one gave one of them up ('' when the kept mesh has both, or nothing was measured)."""
    tried = [s for s in record.get("tried") or [] if not s.get("skipped") and s.get("max_non_ortho") is not None]
    cands = [record.get("before") or {}, *tried]
    if not any("layer_area_at_plan_pct" in c for c in cands):
        return ""

    def both(c: dict) -> bool:
        no = _num(c.get("max_non_ortho"))
        return no is not None and no <= HARD_NON_ORTHO and not c.get("layers_lost") and not c.get("fatal")
    if any(both(c) for c in cands):
        return ""
    after = record.get("after") or {}
    at = after.get("layer_area_at_plan_pct")
    no = _num(after.get("max_non_ortho"))
    if no is not None and no > HARD_NON_ORTHO:
        return (f"The resolved wall could not be built inside the limits: the mesh that keeps the planned first layer "
                f"has a worst cell of {no:.1f} degrees, past the {HARD_NON_ORTHO:.0f} a solver can correct, and every "
                "mesh inside that limit lost the planned first layer.")
    return ("The resolved wall could not be built inside the limits: the mesh kept is inside them, but its first cell "
            f"is at the planned height on {at if isinstance(at, (int, float)) else 0:.0f}% of the wall only - every "
            "mesh that kept the planned first layer was past the limits.")


def _words(b: dict) -> str:
    fatal = b.get("fatal") or []
    no = b.get("max_non_ortho")
    s = f"worst cell {no:.1f} degrees" if isinstance(no, (int, float)) else "no figure"
    s += f" and {', '.join(fatal)}" if fatal else ", no fatal defect"
    at = b.get("layer_area_at_plan_pct")
    if isinstance(at, (int, float)):
        s += f", the planned first layer on {at:.0f}% of the wall"
    return s


def _brief(q: dict | None) -> dict:
    q = q or {}
    return {k: q.get(k) for k in ("cells", "max_non_ortho", "max_skewness", "skew_faces", "fatal",
                                  "layer_area_at_plan_pct", "layers_lost") if k in q or k in
            ("cells", "max_non_ortho", "max_skewness", "skew_faces", "fatal")}


#: _run's return code for a run its `watch` stopped (StoppedByWatch): not a trip, a measured stop.
RC_STOPPED = -2


def _run(runner, ws: Path, bashrc: str, env: dict, cmd: str, log_name: str,
         timeout: float, *, watch: Callable[[], str] | None = None) -> int:
    """Run `cmd` in `ws` to `log_name`: its return code; -1 when its guard stopped it (or it was
    stuck, or out of memory); RC_STOPPED when `watch` gave a reason to stop it (watched as the native
    runs are, sandbox/safe_exec.run_guarded)."""
    from meshpipeline.sandbox.safe_exec import StoppedByWatch
    kw: dict[str, Any] = {}
    if watch is not None:
        from meshpipeline.contracts.native_time import STALL_WINDOW_S
        kw = {"stall_s": STALL_WINDOW_S, "watch": watch}
    try:
        with (ws / log_name).open("w") as fh:
            proc = runner(["bash", "-lc", f"source {bashrc} >/dev/null 2>&1 && {cmd}"],
                          cwd=str(ws), env=env, stdout=fh, stderr=subprocess.STDOUT,
                          timeout=max(1, int(timeout)), **kw)
        return int(proc.returncode)
    except StoppedByWatch:
        return RC_STOPPED
    except subprocess.TimeoutExpired:
        return -1


def _tail(path: Path, n: int = 6) -> str:
    try:
        return " | ".join(path.read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return ""


def _num(v) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


__all__ = ["HARD_NON_ORTHO", "wall_short", "LAYER_LOSS_PTS", "MIN_GAIN_DEG", "NEAR_EDGE_NON_ORTHO", "layers_at_plan", "PROTECTIVE_LAYER_OPTIMISATION",
           "REMESH_MAX_CELLS", "RESCUE_FILE", "RESCUE_MAX_CELLS", "better", "near_edge", "note",
           "protective_meshdict", "rank", "rescue"]
