# Responsibility: Build an FEA mesh for one builder attempt with no model in the loop: measure the staged solid, compute the plan, check its element count before meshing, run the engine adapter, and pull the fixed lever ladder when a measured check fails.
# Boundaries: deterministic. It authors nothing a model could get wrong and publishes nothing itself (what the user reads comes through finalize and the manifest). AI levers are off (Areen's decision 4, 20 Oct): nothing here asks a model.
# Collaborates with: agents/builder/invoke.py (calls drive through EngineSpec.build_driver_for), engines/fea_plan.py, engines/fea_size_field.py, engines/fea_ladder.py, engines/gmsh/fea_adapter.py (run in the mesh runner), contracts/decision_record.py (every decision cited), engines/preflight.py (the stop before meshing).
"""THE FEA DRIVER (FEA build step 4).

One attempt:

1. measure the staged solid (cad/fea_measure on geometry.step, the metre B-rep every gmsh run
   meshes), so Face_k in the plan is Face_k in the mesh and in the deck;
2. PLAN-fea-17: an assembly (several bodies) is staged as its parts, fused where they are bonded or
   side by side where they only touch, every face named Part_k_Face_j;
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
BUILD_FAILED = "fea_build_failed"
#: The mesher ran past its guard clock: our infrastructure, not the part (no lever is pulled).
GUARD_TRIP = "fea_guard_trip"
#: The gate a guard trip is reported under (ours, never the part's).
GUARD_GATE = "fea_guard"
#: The plan measures every wall of a part up to this many surface triangles (the geometry check
#: stops at cad/fea_measure.MAX_RAY_TRIANGLES for speed; the plan cannot hold an unmeasured wall).
MAX_RAY_TRIANGLES = 5_000_000
#: What the remote mesher receives besides the plan: the solid and the facts the deck reads.
PAYLOAD = (*P.STAGED_SOLIDS, "cad_face_names.json", P.PLAN_FILE, "gmsh_spec.json", "assembly.brep", "assembly.json")
#: Where each pass's deliverables are kept until the best one is chosen.
PASS_FOLDER = "_fea_pass_{n}"  # noqa: S105 - a folder name, not a secret
#: every file a pass writes that the delivery reads: the solver files ride with their mesh, so the
#: pass restored is restored whole (a later, worse pass never leaves its .bdf / .cdb behind)
DELIVERABLES = ("mesh.inp", "mesh.bdf", "mesh.cdb", "mesh.msh", "quality.json", P.PLAN_FILE, "gmsh_spec.json")
#: The zones alone past this many elements are past the limit: the histogram stops counting.
STOP_ELEMENTS = 1.5 * P.ELEMENT_LIMIT


# #
# measure and plan (in the worker, pure but for the reading of the staged solid)
# #

def staged_solid(ws: Path) -> Path | None:
    """PLAN-fea-15: the staged solid the FEA path reads - gmsh's own B-rep file when the staging
    wrote one, else the STEP; None when neither is there."""
    return next((ws / n for n in P.STAGED_SOLIDS if (ws / n).exists()), None)


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

    step = staged_solid(ws) or ws / "geometry.step"
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
    try:
        record, surface = measure_cad_surface(shape, mm_per_unit=mm_per_unit, max_ray_triangles=MAX_RAY_TRIANGLES)
    except Exception as exc:  # noqa: BLE001 - healed and measured again; a second failure is the part's
        # PLAN-fea-15: A SHAPE THE MEASURE CANNOT READ is healed (OpenCASCADE's ShapeFix: wires,
        # edges, faces, shells) and measured again, so the part still gets a plan
        from OCP.ShapeFix import ShapeFix_Shape

        logger.warning("fea driver: the staged solid could not be measured as read (%s) - healing it", exc)
        fix = ShapeFix_Shape(shape)
        fix.Perform()
        record, surface = measure_cad_surface(fix.Shape(), mm_per_unit=mm_per_unit,
                                              max_ray_triangles=MAX_RAY_TRIANGLES)
        record.setdefault("notes", []).append(f"the CAD was healed before it was measured ({exc})")
    return record, surface, diag_m


def connection_of(state) -> str:
    """How an assembly's parts are joined, as the user answered on the stage (state fea_connection):
    bonded unless they said contact."""
    from meshpipeline.cad.fea_assembly import BONDED, CONTACT

    said = str((state or {}).get("fea_connection") or "").strip().lower()
    return CONTACT if said == CONTACT else BONDED


def prepare_assembly(ws: Path, record: dict, surface, *, connection: str, source_path: str = "") -> dict:
    """PLAN-fea-17: an assembly's record for the plan, and its staged file for the mesher. Every
    face is renamed Part_k_Face_j (on the record and on the surface the size field reads), each part
    gets its own base size, and the parts are staged as the connection asks (cad/fea_assembly.stage).
    Returns the record (the same dict, renamed in place)."""
    import numpy as np

    from meshpipeline.cad import fea_assembly as A
    from meshpipeline.cad.scout import read_cad

    staged = staged_solid(ws) or ws / "geometry.step"
    shape = read_cad(staged)
    parts = A.solids(shape)
    names: list[str] = [""] * len(parts)
    notes: list[str] = []
    if source_path and Path(source_path).exists():
        try:
            names = A.match_body_names(A.read_body_names(source_path), parts)
        except Exception as exc:  # noqa: BLE001 - a courtesy: every part is still Part_k
            logger.info("fea driver: the CAD's part names were not read (%s)", exc)
    global_names = A.global_face_names(shape)
    renamed = A.rename_record(record, global_names)
    record.clear()
    record.update(renamed)
    if len(surface.names) == len(global_names):
        surface.names = list(global_names)
    sidecar = A.stage(shape, connection, ws, names=names)
    # each part's own size, read off the measured surface (millimetres): its faces' triangles
    bases = []
    for p in range(1, len(parts) + 1):
        faces = [n for n in record.get("faces") or {} if A.part_of_face(n) == p]
        labels = [k for k, n in enumerate(surface.names) if A.part_of_face(n) == p]
        tri = surface.tris[np.isin(surface.label, labels)] if labels else np.zeros((0, 3), dtype=np.int64)
        if not len(tri) or not faces:
            continue
        pts = surface.xyz[np.unique(tri)]
        diag = float(np.linalg.norm(pts.max(axis=0) - pts.min(axis=0)))
        bases.append({"n": p, "id": A.part_id(p), "name": names[p - 1] if p - 1 < len(names) else "",
                      "diag_mm": round(diag, 6), "faces": sorted(faces, key=lambda f: int(f.rsplit("_", 1)[-1]))})
    record["part_bases"] = bases
    record["assembly_plan"] = {"staged": A.STAGED, "sidecar": A.SIDECAR, "connection": connection,
                               "mode": sidecar["mode"], "parts": sidecar["parts"], "pairs": len(sidecar["pairs"]),
                               "interfaces": len(sidecar["interfaces"]), "notes": notes + list(sidecar["notes"])}
    return record


def restage_tied(ws: Path, record: dict) -> dict:
    """PLAN-fea-17's way on for a bonded assembly no pass could mesh as one: the same parts staged
    apart and tied where they touch (cad/fea_assembly.stage, tie); the record's assembly says so."""
    from meshpipeline.cad import fea_assembly as A
    from meshpipeline.cad.scout import read_cad

    shape = read_cad(staged_solid(ws) or ws / "geometry.step")
    try:
        names = [str(p.get("name") or "") for p in json.loads((ws / A.SIDECAR).read_text()).get("parts") or []]
    except (OSError, ValueError):
        names = []
    sidecar = A.stage(shape, A.BONDED, ws, names=names, tie=True)
    plan = dict(record.get("assembly_plan") or {})
    plan.update(mode=sidecar["mode"], pairs=len(sidecar["pairs"]), notes=list(sidecar["notes"]))
    record["assembly_plan"] = plan
    return record


def _histogram(surface, zone_list: list[dict], levers: list[dict], diag_m: float):
    from meshpipeline.engines.fea_size_field import SizeField

    field = SizeField(surface, zone_list, growth=L.growth(levers), scale_to_m=1e-3)
    return field.histogram(coarsest=diag_m / P.MIN_DIVISIONS, per_h3=P.PER_H3, stop_elements=STOP_ELEMENTS)


def make_plan(record: dict, surface, diag_m: float, *, levers: list[dict] | None = None,
              limit: int = P.ELEMENT_LIMIT, zone_floor_m: float | None = None, hist=None,
              through: float | None = None) -> dict:
    """The plan for this part, with the ladder's levers applied: the zones the rules ask for, the
    coarsest base the part allows, the estimate against `limit`. `zone_floor_m`: PLAN-fea-8's
    coarsening (the finest zones floored, never past their validity minimums); `hist`: the zones'
    histogram when the caller already has it."""
    levers = list(levers or [])
    zone_list = P.zones(record, tighten=L.tightened(levers))
    if zone_floor_m:
        zone_list = P.floored(zone_list, float(zone_floor_m), through or P.KEEP_THROUGH)
    if hist is None:
        hist = _histogram(surface, zone_list, levers, diag_m)
    plan = P.finish(record, zone_list, hist, diag_m=diag_m, limit=limit, levers=levers, growth=L.growth(levers))
    if zone_floor_m:
        plan["zone_floor_m"] = float(zone_floor_m)
        plan["resolution_short"] = P.shortfalls(zone_list)
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


#: PLAN-fea-8's coarsening search: the zone floor doubles from the finest zone this many times at
#: most, then one step back where that still fits.
FLOOR_STEPS = 14


def complexity(record: dict) -> str:
    """The part's complexity class, for its fill rate (PLAN-fea-13): measured, never guessed - the
    CAD faces, the hot spots and the micro details. Complex geometry builds slower per element than
    simple geometry of the same element count (Areen, 21 Oct)."""
    faces = len(record.get("faces") or {})
    spots = len(record.get("hot_spots") or [])
    micro = len(record.get("micro") or [])
    if faces > COMPLEX_FACES or spots > COMPLEX_SPOTS or micro:
        return "complex"
    if faces <= SIMPLE_FACES and spots <= SIMPLE_SPOTS:
        return "simple"
    return "moderate"


def fill_rate(record: dict) -> tuple[float, str]:
    """(elements per second of one native pass at the slow end of the part's class, the class)."""
    cls = complexity(record)
    return FILL_RATES[cls], cls


def runnable_elements(ceiling_s: float | None, rate: float | None = None) -> int | None:
    """The most elements one pass can plan so that GUARD_FACTOR times its planned time, at `rate`,
    still fits under `ceiling_s` (the mesh job's limit, or what the attempt has left); None when
    either is unknown."""
    rate = rate if rate is not None else min(FILL_RATES.values())
    if not rate or not ceiling_s:
        return None
    return int(rate * float(ceiling_s) / GUARD_FACTOR)


def memory_elements(memory_gib: float | None = None) -> int:
    """PLAN-fea-16: the most elements the mesher's peak memory, with MEMORY_MARGIN to spare, fits in
    `memory_gib` - by the measured model peak = MEMORY_BASE_MB + BYTES_PER_ELEMENT x elements."""
    gib = MESH_JOB_MEMORY_GIB if memory_gib is None else float(memory_gib)
    room_mb = gib * 1024.0 / MEMORY_MARGIN - MEMORY_BASE_MB
    return max(0, int(room_mb * 1024.0 * 1024.0 / BYTES_PER_ELEMENT))


def element_limit(record: dict, ceiling_s: float | None) -> tuple[int, str]:
    """PLAN-fea-8's limit for this part: ELEMENT_LIMIT, or less when the mesher could not build that
    many inside its time guard at the part's slow fill rate (PLAN-fea-13), or hold them in the mesh
    job's memory (PLAN-fea-16); and why."""
    rate, cls = fill_rate(record)
    runnable = runnable_elements(ceiling_s, rate)
    memory = memory_elements()
    limit, why = P.ELEMENT_LIMIT, "the element limit"
    if runnable is not None and runnable < limit:
        limit, why = runnable, (f"what the mesher builds within its time limit ({rate:g} elements a second for a "
                                f"{cls} part, {GUARD_FACTOR:g} times over, inside {int(ceiling_s or 0)} s)")
    if memory < limit:
        limit, why = memory, (f"what fits in the mesh job's {MESH_JOB_MEMORY_GIB:g} GiB of memory "
                              f"({BYTES_PER_ELEMENT:g} bytes an element, with {MEMORY_MARGIN:g} times to spare)")
    return limit, why


def limit_facts() -> dict:
    """PLAN-fea-16's numbers, for the why record."""
    return {"memory_gib": MESH_JOB_MEMORY_GIB, "bytes_per_element": BYTES_PER_ELEMENT,
            "base_mb": MEMORY_BASE_MB, "margin": MEMORY_MARGIN, "memory_elements": memory_elements()}


def choose_plan(record: dict, surface, diag_m: float, *, levers: list[dict] | None = None,
                ceiling_s: float | None = None) -> dict:
    """PLAN-fea-8, NEVER A STOP BEFORE MESHING (Areen, 21 Oct): the rules' own plan when it fits under
    the limit; else the finest zones coarsened until it fits, never past what keeps elements valid,
    what that leaves short named (resolution_short); else - every zone at its validity minimum and
    still over - meshed so anyway. The returned plan always meshes."""
    levers = list(levers or [])
    limit, why = element_limit(record, ceiling_s)
    zone_list = P.zones(record, tighten=L.tightened(levers))
    plan = make_plan(record, surface, diag_m, levers=levers, limit=limit)
    plan["limit"].update(why=why, memory=limit_facts())
    if plan["fits"]:
        return _restate(record, plan)
    needed = int(plan["estimate"]["elements"])
    said = f"about {needed:,} elements" if plan.get("histogram_complete", True) else f"more than {needed:,} elements"
    floor = min([float(z["size_m"]) for z in zone_list] or [diag_m / P.MIN_DIVISIONS])
    best = plan
    last: list[float] | None = None
    for _ in range(FLOOR_STEPS):
        floor *= 2.0
        sizes = [float(z["size_m"]) for z in P.floored(zone_list, floor)]
        if sizes == last:
            # every zone the floor reaches is already at its validity minimum: a coarser floor
            # changes nothing, so the search stops here (fea0221 planned fourteen identical
            # histograms, 330 s, before its first pass)
            break
        last = sizes
        best = make_plan(record, surface, diag_m, levers=levers, limit=limit, zone_floor_m=floor)
        if best["fits"]:
            break
    at_minimums = not best["fits"]
    if at_minimums:
        # EVEN AT THEIR VALIDITY MINIMUMS the zones need more than the limit: meshed so anyway - the
        # least resolution that keeps every element valid - and said
        best["fits"] = True
    else:
        # one step back: a floor a quarter lower still fits? then it gives back resolution
        finer = make_plan(record, surface, diag_m, levers=levers, limit=limit, zone_floor_m=0.75 * floor)
        if finer["fits"]:
            best, floor = finer, 0.75 * floor
    best["limit"].update(why=why, memory=limit_facts())
    best["coarsened"] = {"needed": needed, "needed_words": said, "zone_floor_m": floor, "at_minimums": at_minimums,
                         # PLAN-fea-16: even the least valid mesh may not fit in the job's memory - meshed anyway
                         "over_memory": bool(at_minimums and int(best["estimate"]["elements"]) > memory_elements())}
    return _restate(record, best)


def _restate(record: dict, plan: dict) -> dict:
    """The plan's words and decisions again, now that it says what was coarsened."""
    plan["words"] = P.words(record, plan)
    plan["decisions"] = P.decisions(record, plan)
    return plan


#: PLAN-fea-13: THE FILL RATE BY COMPLEXITY - elements per second of one native gmsh pass, at the
#: slow end of each class, measured on the FEA lab VMs (8 vCPU, the mesh job's 16 GiB, gmsh on one
#: thread). MEASURED 22 Oct on 12 lab parts of 68k-1.3M elements, after the deck writers matched
#: every CAD face in one pass (they had sorted the whole mesh once per face: a 446-face sprocket
#: built at 165 a second, now 1,539): the slowest of each class was simple 1,779, moderate 1,243,
#: complex 1,539 elements a second. Each rate sits under its class's slowest with margin.
FILL_RATES: dict[str, float] = {"simple": 1500.0, "moderate": 1100.0, "complex": 1000.0}
#: the class boundaries (CAD faces, hot spots); any micro detail makes a part complex
SIMPLE_FACES, SIMPLE_SPOTS = 40, 20
COMPLEX_FACES, COMPLEX_SPOTS = 200, 100
#: PLAN-fea-16: THE MEMORY LIMIT. The mesh job's memory (deploy/gcp MESH_MEMORY: 32 GiB on dev and
#: prod, the Cloud Run most at 8 vCPU) and the mesher's peak, MEASURED on the lab VMs (22 Oct, the
#: gmsh driver's own VmHWM): 450 MB with nothing meshed, and 4.65 KB an element at 2.87M elements
#: (13.3 GB) and 5.04 KB at 2.71M (13.5 GB) - the tetrahedra, their curved midside nodes, the quality
#: arrays and the three solver files at once. BYTES_PER_ELEMENT is the worst of those, rounded up;
#: MEMORY_MARGIN covers the parts the lab did not see. With 32 GiB that is about 5.3M elements.
MESH_JOB_MEMORY_GIB = 32.0
MEMORY_BASE_MB = 450.0
BYTES_PER_ELEMENT = 5100.0
MEMORY_MARGIN = 1.25
#: the guard is GUARD_FACTOR times the planned time (D3's factor), never over the mesh job's own
#: limit (deploy/gcp MESH_TIMEOUT_SECONDS, 14,400 s) less JOB_MARGIN_S, nor what the attempt has left
GUARD_FACTOR = 3.0
MESH_JOB_LIMIT_S = 14_400
JOB_MARGIN_S = 600
#: what an attempt keeps back after the last pass (the deck's writers, the hand-back)
ATTEMPT_RESERVE_S = 300


def guard_seconds(planned_s: float | None, *, floor_s: float, ceiling_s: float,
                  factor: float = GUARD_FACTOR) -> int:
    """D3's TIME-2 arithmetic (#259 work_limits.guard_seconds, mirrored): factor x the planned time,
    never under the floor, never over the ceiling."""
    floor = max(1.0, float(floor_s))
    ceiling = max(floor, float(ceiling_s))
    if planned_s is None:
        return int(floor)
    return int(min(ceiling, max(floor, factor * float(planned_s))))


def ceiling_for(run) -> float:
    """PLAN-fea-13's ceiling for one pass: the mesh job's own limit less its margin, and never more
    than the attempt has left (its deadline, less ATTEMPT_RESERVE_S)."""
    job = float(MESH_JOB_LIMIT_S - JOB_MARGIN_S)
    deadline = getattr(run, "deadline_s", None)
    started = getattr(run, "_started", None)
    if deadline and started is not None:
        left = float(deadline) - (time.monotonic() - float(started)) - ATTEMPT_RESERVE_S
        return max(60.0, min(job, left))
    return job


def planned_work(plan: dict, cap_s: float, *, rate: float | None = None, cls: str = "",
                 ceiling_s: float | None = None) -> dict:
    """The pass's planned work and its guard clock, in D3's terms (#259, contracts/work_limits:
    PlannedWork / guard_for). PLANNED WORK DECIDES, THE CLOCK ONLY GUARDS (PLAN-fea-13): the guard
    is GUARD_FACTOR times the planned time at the part's slow fill rate, never under the engine's
    old fixed cap (the floor) and never over the mesh job's limit or what the attempt has left.
    REBASE NOTE: once #259 merges this becomes `work_limits.PlannedWork(where="fea.gmsh",
    unit="elements", ...)` and a trip is recorded with `determinism.note_guard_trip`."""
    amount = float((plan.get("estimate") or {}).get("elements") or 0)
    rate = rate if rate is not None else min(FILL_RATES.values())
    planned_s = amount / rate if rate and amount > 0 else None
    ceiling = float(ceiling_s if ceiling_s is not None else cap_s)
    guard = guard_seconds(planned_s, floor_s=float(cap_s), ceiling_s=ceiling)
    return {"where": "fea.gmsh", "unit": "elements", "amount": amount or None, "rate": rate, "class": cls,
            "floor_s": float(cap_s), "ceiling_s": ceiling, "planned_s": planned_s, "guard_s": guard,
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
                   "out_of_memory": bool(passes and passes[-1].get("out_of_memory")),
                   "estimate": int((passes[-1].get("estimate") or 0) if passes else 0),
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
    if passes and passes[-1].get("out_of_memory"):
        # LAD-fea-7
        return (f"the mesher ran out of memory building about {int(passes[-1].get('estimate') or 0):,} elements "
                f"({len(passes)} pass(es); levers {tried}) - this plan needs more memory than the mesh job has; "
                "another attempt on the same machine runs out the same way.")
    readings = [float(p["min_sicn"]) for p in passes if p.get("min_sicn") is not None]
    worst = min(readings) if readings else None
    return (f"no pass of the FEA plan built a valid mesh - a degenerate or inverted element ({len(passes)} "
            f"pass(es); levers {tried}; best worst element {worst}). The plan is deterministic: another attempt "
            "builds the same mesh.")


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
    if staged_solid(ws) is None:
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

    staged = staged_solid(ws)
    healed = any("healed" in str(n) for n in record.get("notes") or [])
    _cite(job_id, [{"stage": "fea_plan", "decided": "staged solid", "value": staged.name if staged else None,
                    "rule": "PLAN-fea-15", "inputs": {"healed": healed},
                    "words": (f"The part was read from {staged.name if staged else 'the staged file'}"
                              + (", healed before it was measured." if healed else "."))}])
    if int(record.get("bodies") or 1) > 1:
        # PLAN-fea-17: AN ASSEMBLY - its parts staged as the user said they are joined (bonded by
        # default), every face Part_k_Face_j, each part sized by its own rules
        connection = connection_of(state)
        try:
            record = await asyncio.to_thread(prepare_assembly, ws, record, surface, connection=connection,
                                             source_path=source_path)
        except Exception as exc:  # noqa: BLE001 - recorded as the attempt's reason
            logger.exception("fea driver: the assembly could not be staged - job_id=%s", job_id)
            _refuse(ws, cause=FailureCause.NOT_BUILT.value, text=f"the assembly could not be staged ({exc})",
                    reason=f"the assembly could not be staged ({type(exc).__name__})")
            await run.fence("deliver builder outcome")
            outcome = run.outcome(produced_deliverable=False, failure_marker=BUILD_FAILED)
            return False, BUILD_FAILED, outcome
        asm = record.get("assembly_plan") or {}
        _cite(job_id, [{"stage": "fea_plan", "decided": "assembly", "value": asm.get("mode"),
                        "rule": "PLAN-fea-17",
                        "inputs": {"bodies": record.get("bodies"), "connection": connection,
                                   "pairs": asm.get("pairs"), "interfaces": asm.get("interfaces")},
                        "words": " ".join(P.assembly_words(asm)[:2])}])
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
    rate, cls = fill_rate(record)
    chosen: dict = {}
    n0 = 0
    while True:
        for n in range(n0 + 1, n0 + L.MAX_PASSES + 1):
            if n == n0 + 1:
                # PLAN-fea-8: the rules' own plan, coarsened to fit under the limit when it must - never a
                # stop before meshing
                plan = await asyncio.to_thread(choose_plan, record, surface, diag_m, levers=levers,
                                               ceiling_s=ceiling_for(run))
                chosen = {"limit": plan["limit"], "zone_floor_m": plan.get("zone_floor_m"),
                          "coarsened": plan.get("coarsened")}
            else:
                floor_m = chosen["zone_floor_m"]
                if L.coarse_factor(levers) < 1.0:
                    # LAD-fea-4: the built mesh was over the limit - the finest zones coarser, from the
                    # floor already in force (or the finest zone), never past their validity minimums
                    start = floor_m or min([float(z["size_m"]) for z in P.zones(record)] or [diag_m / P.MIN_DIVISIONS])
                    floor_m = start / L.coarse_factor(levers)
                plan = await asyncio.to_thread(make_plan, record, surface, diag_m, levers=levers,
                                               limit=int(chosen["limit"]["elements"]), zone_floor_m=floor_m)
                plan["limit"] = chosen["limit"]
                if chosen.get("coarsened"):
                    plan["coarsened"] = chosen["coarsened"]
                    plan["fits"] = True
                plan = _restate(record, plan)
            if n == n0 + 1:
                _cite(job_id, plan["decisions"])
            write_plan(ws, plan)
            run.note_authoring()
            _clear_outputs(ws)
            note_native_pass(ws, n)
            note_native_payload(ws, [p for p in PAYLOAD if (ws / p).exists()])
            # PLANNED WORK DECIDES, THE CLOCK ONLY GUARDS (D3's interface, mirrored - see planned_work)
            work = planned_work(plan, cap, rate=rate, cls=cls, ceiling_s=ceiling_for(run))
            _cite(job_id, [{"stage": "fea_time", "decided": "guard", "value": work["guard_s"], "rule": "PLAN-fea-13",
                            "inputs": {k: work[k] for k in ("amount", "rate", "class", "planned_s", "floor_s",
                                                             "ceiling_s", "factor")} | {"pass": n},
                            "words": (f"Pass {n}: about {int(work['amount'] or 0):,} elements at {work['rate']:g} a second "
                                      f"(a {cls} part) is about {int(work['planned_s'] or 0)} s; the mesher may take "
                                      f"{work['guard_s']} s.")}])
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
            # LAD-fea-7: killed by the job's memory limit (the kernel's SIGKILL on the mesher)
            killed = str(result.get("signal") or "") == "SIGKILL" or "killed by SIGKILL" in str(result.get("log_tail") or "")
            passes.append({**L.pass_facts(n, result.get("rc"), q, plan, log=str(result.get("log_tail") or ""),
                                          killed=killed), "native_s": native_s})
            if made:
                _keep_pass(ws, n)
            lever = L.next_lever(passes[-1], levers, plan)
            if lever is None:
                if passes[-1].get("out_of_memory"):
                    _cite(job_id, [{"stage": "fea_ladder", "decided": "no lever", "value": "out of memory",
                                    "rule": "LAD-fea-7", "inputs": {"pass": n, "estimate": passes[-1].get("estimate")},
                                    "words": "The mesher ran out of memory; no lever builds this plan in less, so no retry."}])
                break
            _cite(job_id, [{"stage": "fea_ladder", "decided": "lever", "value": lever["lever"], "rule": lever["lever"],
                            "inputs": lever.get("inputs") or {}, "words": lever["words"]}])
            levers.append(lever)

        if (tripped is None and passes and not any(f["valid"] for f in passes)
                and (record.get("assembly_plan") or {}).get("mode") == "bonded"):
            # PLAN-fea-17: A BONDED ASSEMBLY THE MESHER COULD NOT BUILD AS ONE (the fused parts left
            # slivers it cannot fill): the parts are meshed apart and tied where they meet - still
            # bonded, never a stop
            record = await asyncio.to_thread(restage_tied, ws, record)
            _cite(job_id, [{"stage": "fea_ladder", "decided": "tied", "value": "tie", "rule": "PLAN-fea-17",
                            "inputs": {"passes": len(passes)},
                            "words": ("No pass built the bonded parts as one mesh, so each part is meshed on its "
                                      "own and tied where they meet.")}])
            n0, levers = len(passes), []
            continue
        break

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


__all__ = ["connection_of", "prepare_assembly", "restage_tied", "BYTES_PER_ELEMENT", "MESH_JOB_MEMORY_GIB", "limit_facts", "memory_elements", "FILL_RATES", "FLOOR_STEPS", "GUARD_FACTOR", "MESH_JOB_LIMIT_S", "ceiling_for", "choose_plan",
           "complexity", "element_limit", "fill_rate", "guard_seconds", "runnable_elements", "BUILD_FAILED", "GUARD_TRIP", "NO_SOURCE", "QUALITY_GATE", "applies", "drive",
           "driver_for", "ladder_facts", "ladder_words", "make_plan", "measure_staged", "planned_work", "write_plan"]
