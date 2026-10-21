# Responsibility: Build an FEA mesh for one builder attempt with no model in the loop: measure the staged solid, compute the plan, check its element count before meshing, run the engine adapter, and pull the fixed lever ladder when a measured check fails.
# Boundaries: deterministic. It authors nothing a model could get wrong and publishes nothing itself (what the user reads comes through finalize and the manifest). AI levers are off (Areen's decision 4, 20 Oct): nothing here asks a model.
# Collaborates with: agents/builder/invoke.py (calls drive through EngineSpec.build_driver_for), engines/fea_plan.py, engines/fea_size_field.py, engines/fea_ladder.py, engines/gmsh/fea_adapter.py (run in the mesh runner), contracts/decision_record.py (every decision cited), engines/preflight.py (the stop before meshing).
"""THE FEA DRIVER (FEA build step 4).

One attempt:

1. measure the staged solid (cad/fea_measure on geometry.step, the metre B-rep every gmsh run
   meshes), so Face_k in the plan is Face_k in the mesh and in the deck;
2. PLAN-fea-9: one part only;
3. compute the plan (engines/fea_plan.py) and its element estimate;
4. PLAN-fea-8: over the detail level, stop BEFORE meshing, with the options (a pre-flight
   refusal: no mesher is started, no time is spent);
5. mesh it (the engine adapter, in the mesh runner), then read the measured checks;
6. a check failed: the next lever of the fixed ladder (engines/fea_ladder.py), at most
   MAX_PASSES passes in the one attempt;
7. LAD-fea-5: the best pass is delivered.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from meshpipeline.engines import fea_ladder as L
from meshpipeline.engines import fea_plan as P

if TYPE_CHECKING:
    from meshpipeline.agents.builder.driver_run import BuilderDriverRun
    from meshpipeline.contracts.event_stream import ExecutionEventPublisher

logger = logging.getLogger(__name__)

ENGINE = "gmsh"
#: Failure markers this driver ends an attempt with.
NO_SOURCE = "fea_source_missing"
NOT_ONE_PART = "fea_not_one_part"
OVER_LEVEL = "fea_over_level"
BUILD_FAILED = "fea_build_failed"
#: The mesher ran past its guard clock: our infrastructure, not the part (no lever is pulled).
GUARD_TRIP = "fea_guard_trip"
#: The gate a guard trip is reported under (ours, never the part's).
GUARD_GATE = "fea_guard"
#: The plan measures every wall of a part up to this many surface triangles (the geometry check
#: stops at cad/fea_measure.MAX_RAY_TRIANGLES for speed; the plan cannot hold an unmeasured wall).
MAX_RAY_TRIANGLES = 5_000_000
#: What the remote mesher receives besides the plan: the solid and the facts the deck reads.
PAYLOAD = ("geometry.step", "cad_face_names.json", P.PLAN_FILE, "gmsh_spec.json")
#: Where each pass's deliverables are kept until the best one is chosen.
PASS_FOLDER = "_fea_pass_{n}"  # noqa: S105 - a folder name, not a secret
#: every file a pass writes that the delivery reads: the solver files ride with their mesh, so the
#: pass restored is restored whole (a later, worse pass never leaves its .bdf / .cdb behind)
DELIVERABLES = ("mesh.inp", "mesh.bdf", "mesh.cdb", "mesh.msh", "quality.json", P.PLAN_FILE, "gmsh_spec.json")
#: The zones alone past this many elements are past every level: the histogram stops counting.
STOP_ELEMENTS = 3.0 * P.LEVELS["max"].target


# #
# measure and plan (in the worker, pure but for the reading of the staged solid)
# #

def _metre_box(step: Path) -> tuple[list[float], float]:
    """The staged solid's box in metres, as gmsh reads it (the reader every gmsh run uses)."""
    import gmsh

    mine = not gmsh.isInitialized()
    if mine:
        gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("Geometry.OCCBoundsUseStl", 1)
        gmsh.model.add("fea_box")
        gmsh.model.occ.importShapes(str(step))
        gmsh.model.occ.synchronize()
        box = list(gmsh.model.getBoundingBox(-1, -1))
        gmsh.model.remove()
    finally:
        if mine:
            gmsh.finalize()
    diag = math.dist(box[:3], box[3:])
    return box, diag


def measure_staged(ws: Path) -> tuple[dict, Any, float]:
    """(the measurement record in mm, its labelled surface in mm, the part's diagonal in m)."""
    from meshpipeline.cad.fea_measure import measure_cad_surface
    from meshpipeline.cad.scout import read_cad

    step = ws / "geometry.step"
    _box, diag_m = _metre_box(step)
    shape = read_cad(step)
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    b = Bnd_Box()
    # THE TIGHT BOX: OpenCASCADE's plain box is padded by the shapes' tolerances, about 6% of an 8 mm
    # nut - every length of a small part's record (walls, radii, the cusps' places) came out short
    BRepBndLib.AddOptimal_s(shape, b, False, False)
    x0, y0, z0, x1, y1, z1 = b.Get()
    diag_read = math.dist((x0, y0, z0), (x1, y1, z1))
    if diag_read <= 0 or diag_m <= 0:
        raise ValueError("the staged solid has no size")
    # the staged STEP is in metres whatever unit its header names: the scale is read off the two
    # readers' boxes, so the record is millimetres either way - and a unit is a power of ten, so the
    # ratio is snapped to one (the two boxes never agree to the last digit)
    ratio = 1000.0 * diag_m / diag_read
    mm_per_unit = 10.0 ** round(math.log10(ratio)) if ratio > 0 else ratio
    record, surface = measure_cad_surface(shape, mm_per_unit=mm_per_unit, max_ray_triangles=MAX_RAY_TRIANGLES)
    return record, surface, diag_m


def _histogram(surface, zone_list: list[dict], levers: list[dict], diag_m: float):
    from meshpipeline.engines.fea_size_field import SizeField

    field = SizeField(surface, zone_list, growth=L.growth(levers), scale_to_m=1e-3)
    return field.histogram(coarsest=diag_m / P.MIN_DIVISIONS, per_h3=P.PER_H3, stop_elements=STOP_ELEMENTS)


def make_plan(record: dict, surface, diag_m: float, *, fidelity: str, levers: list[dict] | None = None,
              level: P.Level | None = None, zone_floor_m: float | None = None, relax: bool = True,
              hist=None) -> dict:
    """The plan for this part at this level (the run's fidelity, or `level` itself), with the
    ladder's levers applied. `zone_floor_m`: PLAN-fea-8's best possible (the finest zones floored);
    `relax`: work out the fewer-at-spots option when it does not fit; `hist`: the zones' histogram
    when the caller already has it (it does not depend on the level)."""
    from meshpipeline.engines.fea_size_field import SizeField

    level = level or P.level_of(fidelity)
    levers = list(levers or [])
    tighten = L.tightened(levers)
    zone_list = P.zones(record, tighten=tighten)
    if zone_floor_m:
        zone_list = P.floored(zone_list, float(zone_floor_m))
    coarsest = diag_m / P.MIN_DIVISIONS
    if hist is None:
        hist = _histogram(surface, zone_list, levers, diag_m)
    plan = P.finish(record, zone_list, hist, level=level, diag_m=diag_m, base_factor=L.base_factor(levers),
                    levers=levers, growth=L.growth(levers))
    if zone_floor_m:
        plan["zone_floor_m"] = float(zone_floor_m)
        plan["resolution_short"] = P.shortfalls(zone_list)
    if not plan["fits"] and relax:
        relaxed = P.zones(record, relaxed=True, tighten=tighten)
        rfield = SizeField(surface, relaxed, growth=L.growth(levers), scale_to_m=1e-3)
        rhist = rfield.histogram(coarsest=coarsest, per_h3=P.PER_H3, stop_elements=STOP_ELEMENTS)
        plan = P.finish(record, zone_list, hist, level=level, diag_m=diag_m, base_factor=L.base_factor(levers),
                        relaxed_hist=rhist, levers=levers, growth=L.growth(levers))
    plan["mesh"] = L.mesh_settings(levers)
    # WHAT THE RESULT SCREEN SHOWS (engines/fea_results.py): every face, and every hot spot where it is
    plan["faces"] = [{"name": n, "kind": (r or {}).get("kind"), "area_mm2": (r or {}).get("area_mm2"),
                       "thickness_min_mm": (r or {}).get("thickness_min_mm")}
                      for n, r in sorted((record.get("faces") or {}).items(), key=lambda kv: P._face_order(kv[0]))]
    plan["hot_spots"] = [{k: h.get(k) for k in ("id", "type", "shape", "faces", "radius_mm", "angle_deg",
                                                "length_mm", "depth_mm", "local_thickness_mm", "capped")
                           if h.get(k) is not None}
                          | {"at_m": [round(float(c) * 1e-3, 9) for c in (h.get("at_mm") or [])]}
                          for h in record.get("hot_spots") or []]
    plan["measure"] = {"faces": len(record.get("faces") or {}), "counts": record.get("counts") or {},
                       "thinnest_wall_mm": (record.get("thickness") or {}).get("min_mm"),
                       "bodies": record.get("bodies"), "notes": list(record.get("notes") or [])}
    return plan


#: PLAN-fea-8's search for the best possible within the largest level: the zone floor doubles from
#: the finest zone this many times at most, then one step back where that still fits.
FLOOR_STEPS = 14


def _needed(plan: dict) -> tuple[int, bool, str]:
    est = int((plan.get("estimate") or {}).get("elements") or 0)
    complete = bool(plan.get("histogram_complete", True))
    return est, complete, (f"about {est:,} elements" if complete else f"more than {est:,} elements")


def runnable_elements(cap_s: float | None) -> int | None:
    """The most elements one pass can build inside the mesher's guard clock (`cap_s`), at the
    measured fill rate with RUNNABLE_MARGIN to spare; None when either is unknown."""
    if not FEA_FILL_RATE or not cap_s:
        return None
    return int(FEA_FILL_RATE * float(cap_s) * RUNNABLE_MARGIN)


def _runnable(level: P.Level, cap: int | None) -> P.Level:
    """The level, its target lowered so its ceiling is no more than the runnable size."""
    if cap is None or level.target * P.OVER <= cap:
        return level
    return P.Level(level.key, level.label, int(cap / P.OVER))


def choose_plan(record: dict, surface, diag_m: float, *, fidelity: str, levers: list[dict] | None = None,
                cap_s: float | None = None) -> dict:
    """PLAN-fea-8, NEVER A STOP FOR BUDGET (Areen, 21 Oct): the plan at the level asked for when it
    fits; else at the next level up that holds every rule (said); else the best possible within the
    largest level - the finest zones floored until it fits, what that leaves short named
    (resolution_short). The returned plan always fits."""
    cap = runnable_elements(cap_s)
    asked = _runnable(P.level_of(fidelity), cap)
    levers = list(levers or [])
    zone_list = P.zones(record, tighten=L.tightened(levers))
    hist = _histogram(surface, zone_list, levers, diag_m)
    plan = make_plan(record, surface, diag_m, fidelity=fidelity, levers=levers, level=asked, relax=False, hist=hist)
    if plan["fits"]:
        return plan
    needed, complete, said = _needed(plan)
    who = {"asked": asked.key, "asked_label": asked.label, "needed": needed, "complete": complete,
           "needed_words": said}
    key = P.NEXT_LEVEL.get(asked.key)
    while key:
        up = make_plan(record, surface, diag_m, fidelity=fidelity, levers=levers,
                       level=_runnable(P.LEVELS[key], cap), relax=False, hist=hist)
        if up["fits"]:
            up["auto_level"] = who
            if up["detail"]["target"] != P.LEVELS[key].target:
                up["runnable_cap"] = {"elements": cap, "rate": FEA_FILL_RATE, "cap_s": cap_s}
            return _restate(record, up)
        key = P.NEXT_LEVEL.get(key)
    largest = _runnable(P.LEVELS[P.LARGEST], cap)
    floor = min([float(z["size_m"]) for z in zone_list] or [diag_m / P.MIN_DIVISIONS])
    best = None
    for _ in range(FLOOR_STEPS):
        floor *= 2.0
        best = make_plan(record, surface, diag_m, fidelity=fidelity, levers=levers, level=largest,
                         zone_floor_m=floor, relax=False)
        if best["fits"]:
            break
    assert best is not None
    if best["fits"]:
        # one step back: the floor half way down still fits? then it gives back resolution
        mid = 0.75 * floor
        finer = make_plan(record, surface, diag_m, fidelity=fidelity, levers=levers, level=largest,
                          zone_floor_m=mid, relax=False)
        if finer["fits"]:
            best, floor = finer, mid
    best["best_possible"] = {**who, "zone_floor_m": floor}
    if largest.target != P.LEVELS[P.LARGEST].target:
        best["runnable_cap"] = {"elements": cap, "rate": FEA_FILL_RATE, "cap_s": cap_s}
    return _restate(record, best)


def _restate(record: dict, plan: dict) -> dict:
    """The plan's words and decisions again, now that it says how its level was chosen."""
    plan["words"] = P.words(record, plan)
    plan["decisions"] = P.decisions(record, plan)
    return plan


#: The FEA fill's slow rate (elements per second of the whole native run). None until the FEA lab
#: (build steps 7-8) has measured one: the guard is then the engine's old fixed cap.
#: Measured 21 Oct on the FEA lab VMs (8 vCPU, 16 GiB cap like the mesh job, gmsh on one thread): 587,
#: 716, 813 and 1,049 elements a second over four ~420k passes; the slowest-but-one, rounded down.
FEA_FILL_RATE: float | None = 600.0
#: A level is never planned above what one pass builds in this share of the guard clock: a plan the
#: clock stops is a guard trip, never a mesh (step 8's VM lab: 1.7M planned at Fine tripped the
#: 1,500 s guard on every part).
RUNNABLE_MARGIN = 0.7
GUARD_FACTOR = 3.0


def planned_work(plan: dict, cap_s: float) -> dict:
    """The pass's planned work and its guard clock, in D3's terms (#259, contracts/work_limits:
    PlannedWork / guard_for). Planned work - the plan's element estimate - decides everything; the
    clock is only a guard, GUARD_FACTOR times the planned time at the slow rate, never under the old
    cap and never over it here (gmsh's floor and ceiling are both its 1,500 s cap).
    REBASE NOTE: once #259 merges this becomes `work_limits.PlannedWork(where="fea.gmsh",
    unit="elements", ...)` and a trip is recorded with `determinism.note_guard_trip`."""
    amount = float((plan.get("estimate") or {}).get("elements") or 0)
    planned_s = amount / FEA_FILL_RATE if FEA_FILL_RATE and amount > 0 else None
    guard = int(cap_s if planned_s is None else min(cap_s, max(cap_s, GUARD_FACTOR * planned_s)))
    return {"where": "fea.gmsh", "unit": "elements", "amount": amount or None, "rate": FEA_FILL_RATE,
            "floor_s": float(cap_s), "ceiling_s": float(cap_s), "planned_s": planned_s, "guard_s": guard,
            "factor": GUARD_FACTOR}


def write_plan(ws: Path, plan: dict) -> None:
    (ws / P.PLAN_FILE).write_text(json.dumps(plan, indent=1, sort_keys=True))
    # the re-runnable record the deliverable has always carried, in the AI spec's own words
    (ws / "gmsh_spec.json").write_text(json.dumps({
        "element_order": int(plan["element"]["order"]),
        "size": {"mode": "absolute", "value": plan["base_size_m"]},
        "optimize": True}, indent=1))


# #
# the attempt
# #

def _cite(job_id: str, entries: list[dict]) -> None:
    from meshpipeline.contracts import decision_record as dr

    for e in entries:
        dr.cite(job_id, stage=str(e.get("stage")), decided=str(e.get("decided")), value=e.get("value"),
                rule=str(e.get("rule")), inputs=e.get("inputs") or {}, alternatives=e.get("alternatives") or (),
                source=str(e.get("source") or dr.SOURCE_RULE), words=str(e.get("words") or ""))


def _refuse(ws: Path, *, cause: str, text: str, gate: str = "fea_plan", **facts) -> None:
    from meshpipeline.engines.preflight import PreflightRefusal

    base = {"engine": "gmsh", "deterministic": True}
    if gate == "fea_plan":
        base["before_meshing"] = True
    PreflightRefusal(gate=gate, cause=cause, builder_text=f"[{gate.upper()}] {text}",
                     facts={**base, **facts}).write(ws)


#: The gate an exhausted ladder is reported under (the FEA quality floor it could not meet).
QUALITY_GATE = "fea_quality"


def ladder_facts(ws: Path, passes: list[dict], levers: list[dict]) -> dict:
    """What the user's sentence and the retry policy read when the ladder ran out: that it is
    deterministic and exhausted, how many passes and which levers, and where the worst elements
    are (millimetres) with how many sit under the floor."""
    q = _read_quality(ws)
    fq = (q.get("fea") or {}).get("quality") or {}
    where = (fq.get("worst_at_m") or {}).get("scaled_jacobian")
    facts: dict = {"fea": True, "ladder_exhausted": True, "passes": len(passes),
                   "levers": [lv["lever"] for lv in levers],
                   "under_floor": int(q.get("elements_under_floor") or 0),
                   "elements": int(q.get("cells") or 0)}
    if isinstance(where, list) and len(where) == 3:
        facts["worst_at_mm"] = [round(float(v) * 1000.0, 1) for v in where]
    if (q.get("bad_spread_mm")) is not None:
        facts["spread_mm"] = q["bad_spread_mm"]
    return facts


def ladder_words(passes: list[dict], levers: list[dict]) -> str:
    """The builder-facing line for an exhausted ladder (the user's sentence is failure_cause's)."""
    tried = ", ".join(lv["lever"] for lv in levers) or "no lever"
    readings = [float(p["min_sicn"]) for p in passes if p.get("min_sicn") is not None]
    worst = min(readings) if readings else None
    return (f"no pass of the FEA plan met the element quality floor ({len(passes)} pass(es); levers {tried}; "
            f"best worst element {worst}). The plan is deterministic: another attempt builds the same mesh.")


def _read_quality(ws: Path) -> dict:
    try:
        return json.loads((ws / "quality.json").read_text())
    except (OSError, ValueError):
        return {}


def _keep_pass(ws: Path, n: int) -> None:
    d = ws / PASS_FOLDER.format(n=n)
    d.mkdir(exist_ok=True)
    for name in DELIVERABLES:
        if (ws / name).exists():
            shutil.copy2(ws / name, d / name)


def _restore_pass(ws: Path, n: int) -> None:
    d = ws / PASS_FOLDER.format(n=n)
    for name in DELIVERABLES:
        if (d / name).exists():
            shutil.copy2(d / name, ws / name)
        else:
            (ws / name).unlink(missing_ok=True)      # the delivered pass did not write it


def _clear_outputs(ws: Path) -> None:
    for name in ("mesh.inp", "mesh.bdf", "mesh.cdb", "mesh.msh", "quality.json"):
        (ws / name).unlink(missing_ok=True)


async def drive(workspace, state, *, job_id: str, publish: ExecutionEventPublisher,
                run: BuilderDriverRun, source_path: str = ""):
    from meshpipeline.contracts.failure_cause import FailureCause
    from meshpipeline.contracts.mesh_execution import note_native_pass, note_native_payload
    from meshpipeline.engines.registry import get_spec
    from meshpipeline.engines.runtime import get_engine

    ws = Path(workspace)
    fidelity = str(state.get("effective_mesh_fidelity", "") or "")
    if not (ws / "geometry.step").exists():
        logger.warning("fea driver: no staged solid in %s - job_id=%s", ws, job_id)
        await run.fence("deliver builder outcome")
        outcome = run.outcome(produced_deliverable=False, failure_marker=NO_SOURCE)
        return False, NO_SOURCE, outcome

    try:
        record, surface, diag_m = await asyncio.to_thread(measure_staged, ws)
    except Exception as exc:  # noqa: BLE001 - recorded as the attempt's reason
        logger.exception("fea driver: the staged solid could not be measured - job_id=%s", job_id)
        _refuse(ws, cause=FailureCause.NOT_BUILT.value, text=f"the part could not be measured ({exc})",
                reason=f"the part could not be measured ({type(exc).__name__})")
        await run.fence("deliver builder outcome")
        outcome = run.outcome(produced_deliverable=False, failure_marker=BUILD_FAILED)
        return False, BUILD_FAILED, outcome

    alone = P.one_part(record)
    _cite(job_id, [{"stage": "fea_plan", "decided": "one part", "value": not alone, "rule": "PLAN-fea-9",
                    "inputs": {"bodies": record.get("bodies")},
                    "words": alone or "One solid body: meshed as one part."}])
    if alone:
        _refuse(ws, cause=FailureCause.NOT_BUILT.value, text=alone, reason=alone, setup=True)
        await run.fence("deliver builder outcome")
        outcome = run.outcome(produced_deliverable=False, failure_marker=NOT_ONE_PART)
        return False, NOT_ONE_PART, outcome
    if not record.get("faces") or not any("thickness_min_mm" in (r or {}) for r in record["faces"].values()):
        # PLAN-fea-3 cannot be planned on walls nobody measured: stop before meshing rather than
        # deliver a mesh whose walls were never held to the rule
        why = "the part's walls could not be measured, so the elements through them cannot be planned"
        _refuse(ws, cause=FailureCause.NOT_BUILT.value, text=why, reason=why)
        await run.fence("deliver builder outcome")
        outcome = run.outcome(produced_deliverable=False, failure_marker=BUILD_FAILED)
        return False, BUILD_FAILED, outcome

    levers: list[dict] = []
    passes: list[dict] = []
    tripped: dict | None = None
    policy = get_spec(ENGINE).run_policy
    cap = int(policy.run_timeout()) if policy is not None else 1500
    chosen: dict = {}
    for n in range(1, L.MAX_PASSES + 1):
        if n == 1:
            # PLAN-fea-8: the level asked for, the next that holds every rule, or the best possible
            # within the largest - never a stop before meshing for budget
            plan = await asyncio.to_thread(choose_plan, record, surface, diag_m, fidelity=fidelity, levers=levers,
                                           cap_s=cap)
            chosen = {"level": P.Level(plan["detail"]["key"], plan["detail"]["label"], plan["detail"]["target"]),
                      "zone_floor_m": plan.get("zone_floor_m"), "runnable_cap": plan.get("runnable_cap"),
                      "auto_level": plan.get("auto_level"), "best_possible": plan.get("best_possible")}
        else:
            plan = await asyncio.to_thread(make_plan, record, surface, diag_m, fidelity=fidelity, levers=levers,
                                           level=chosen["level"], zone_floor_m=chosen["zone_floor_m"])
            for k in ("auto_level", "best_possible", "runnable_cap"):
                if chosen.get(k):
                    plan[k] = chosen[k]
            plan = _restate(record, plan)
        if n == 1:
            _cite(job_id, plan["decisions"])
        write_plan(ws, plan)
        run.note_authoring()
        if not plan["fits"]:
            if passes:
                break                                  # a lever cannot buy past the level: keep what was built
            est = plan["estimate"]["elements"]
            _refuse(ws, cause=FailureCause.CELL_BUDGET.value,
                    text=" ".join(w for w in plan["words"] if w.startswith(("Stopped", "Option", "No level"))),
                    cells=est, limit=plan["detail"]["ceiling"], level=plan["detail"]["label"],
                    complete=plan.get("histogram_complete", True),
                    options=[o["words"] for o in plan.get("options") or []], fea=True)
            await run.fence("deliver builder outcome")
            outcome = run.outcome(produced_deliverable=False, failure_marker=OVER_LEVEL)
            return False, OVER_LEVEL, outcome
        _clear_outputs(ws)
        note_native_pass(ws, n)
        note_native_payload(ws, [p for p in PAYLOAD if (ws / p).exists()])
        # PLANNED WORK DECIDES, THE CLOCK ONLY GUARDS (D3's interface, mirrored - see planned_work)
        work = planned_work(plan, cap)
        await run.fence("start native mesh")
        native_t0 = time.monotonic()
        result = await asyncio.to_thread(get_engine(ENGINE).run_cartesian_mesh, ws, timeout=work["guard_s"])
        native_s = round(time.monotonic() - native_t0, 1)
        await run.fence("accept native mesh output")
        if result.get("timed_out"):
            # A GUARD TRIP is our infrastructure, never the part's: it pulls no lever and changes no
            # plan. The attempt ends here; the run's own retry takes it again (D3: replayed once)
            run.note_native_run(produced_usable_mesh=False)
            tripped = {**work, "pass": n}
            break
        q = _read_quality(ws) if result.get("rc") in (0, 4) else {}
        made = bool(q) and (ws / "mesh.inp").exists()
        run.note_native_run(produced_usable_mesh=made and result.get("rc") == 0)
        passes.append({**L.pass_facts(n, result.get("rc"), q, plan), "native_s": native_s})
        if made:
            _keep_pass(ws, n)
        lever = L.next_lever(passes[-1], levers, plan)
        if lever is None:
            break
        _cite(job_id, [{"stage": "fea_ladder", "decided": "lever", "value": lever["lever"], "rule": lever["lever"],
                        "inputs": lever.get("inputs") or {}, "words": lever["words"]}])
        levers.append(lever)

    if tripped is not None and not any(f["valid"] for f in passes):
        (ws / "fea_passes.json").write_text(json.dumps({"passes": passes, "delivered": None, "levers": levers,
                                                        "guard_trip": tripped}, indent=1))
        logger.warning("fea driver: the mesher ran past its guard clock (%ss, pass %s) - job_id=%s",
                       tripped["guard_s"], tripped["pass"], job_id)
        # told as OURS (run_infrastructure: the run may be taken again), never as the part's detail
        # level. REBASE NOTE (#259): becomes determinism.note_guard_trip, which the graph replays once
        _refuse(ws, cause=FailureCause.RUN_INFRASTRUCTURE.value, gate=GUARD_GATE,
                text=f"the mesher ran past its guard clock ({tripped['guard_s']} s, pass {tripped['pass']})",
                deterministic=False, ours=True, guard_trip=tripped)
        await run.fence("deliver builder outcome")
        outcome = run.outcome(produced_deliverable=False, exhausted=False, failure_marker=GUARD_TRIP)
        return False, GUARD_TRIP, outcome
    best, rule = L.best_pass(passes)
    _cite(job_id, [{"stage": "fea_ladder", "decided": "delivered pass", "value": best, "rule": "LAD-fea-5",
                    "inputs": {"passes": passes, "deciding": rule},
                    "words": (f"Pass {best} of {len(passes)} delivered." if best else "No pass built a valid mesh.")}])
    ok = best is not None
    if best is not None:
        _restore_pass(ws, best)
    (ws / "fea_passes.json").write_text(json.dumps({"passes": passes, "delivered": best, "levers": levers}, indent=1))
    if not ok and passes:
        # THE LADDER RAN OUT on a mesh it built: every pass this plan allows was tried, so the same
        # part at the same level builds the same mesh again. Said as the FEA's own outcome; the
        # executor still judges the mesh on disk, and this record tells the retry policy (and the
        # user's sentence) that another attempt on this engine cannot change it (job ab20566f
        # spent a second identical 14-minute attempt)
        _refuse(ws, cause=FailureCause.MESH_QUALITY.value, gate=QUALITY_GATE,
                text=ladder_words(passes, levers), **ladder_facts(ws, passes, levers))
    await run.fence("deliver builder outcome")
    outcome = run.outcome(produced_deliverable=ok, exhausted=not ok, failure_marker="" if ok else BUILD_FAILED)
    return outcome.produced_deliverable, (outcome.terminal_value or BUILD_FAILED), outcome


def applies(state) -> bool:
    """Whether this run is the FEA driver's: a structural purpose, a 3D solid, and no face the user
    named with a role (version 1 names every face Face_k for the user's own solver; a named
    support or load still goes through the builder loop, which maps the words onto faces). A 2D
    plane-stress case keeps the builder loop."""
    s = state or {}
    if str(s.get("purpose") or "").strip() != "structural":
        return False
    if str(s.get("dimensionality") or "").strip().upper() == "2D":
        return False
    return not any(isinstance(p, dict) and str(p.get("name") or "").strip() for p in s.get("intake_patches") or [])


def driver_for(state):
    """engines/gmsh/spec's run driver (EngineSpec.build_driver_for): this driver, or None."""
    return drive if applies(state) else None


__all__ = ["FLOOR_STEPS", "RUNNABLE_MARGIN", "choose_plan", "runnable_elements", "BUILD_FAILED", "GUARD_TRIP", "NOT_ONE_PART", "NO_SOURCE", "OVER_LEVEL", "QUALITY_GATE", "applies", "drive",
           "driver_for", "ladder_facts", "ladder_words", "make_plan", "measure_staged", "planned_work", "write_plan"]
