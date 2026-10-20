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
#: What the remote mesher receives besides the plan: the solid and the facts the deck reads.
PAYLOAD = ("geometry.step", "cad_face_names.json", P.PLAN_FILE, "gmsh_spec.json")
#: Where each pass's deliverables are kept until the best one is chosen.
PASS_FOLDER = "_fea_pass_{n}"  # noqa: S105 - a folder name, not a secret
DELIVERABLES = ("mesh.inp", "mesh.msh", "quality.json", P.PLAN_FILE, "gmsh_spec.json")
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
    BRepBndLib.Add_s(shape, b, True)
    x0, y0, z0, x1, y1, z1 = b.Get()
    diag_read = math.dist((x0, y0, z0), (x1, y1, z1))
    if diag_read <= 0 or diag_m <= 0:
        raise ValueError("the staged solid has no size")
    # the staged STEP is in metres whatever unit its header names: the scale is read off the two
    # readers' boxes, so the record is millimetres either way
    mm_per_unit = 1000.0 * diag_m / diag_read
    record, surface = measure_cad_surface(shape, mm_per_unit=mm_per_unit)
    return record, surface, diag_m


def make_plan(record: dict, surface, diag_m: float, *, fidelity: str, levers: list[dict] | None = None,
              level: P.Level | None = None) -> dict:
    """The plan for this part at this level (the run's fidelity, or `level` itself), with the
    ladder's levers applied."""
    from meshpipeline.engines.fea_size_field import SizeField

    level = level or P.level_of(fidelity)
    levers = list(levers or [])
    tighten = L.tightened(levers)
    zone_list = P.zones(record, tighten=tighten)
    coarsest = diag_m / P.MIN_DIVISIONS
    field = SizeField(surface, zone_list, growth=L.growth(levers), scale_to_m=1e-3)
    hist = field.histogram(coarsest=coarsest, per_h3=P.PER_H3, stop_elements=STOP_ELEMENTS)
    plan = P.finish(record, zone_list, hist, level=level, diag_m=diag_m, base_factor=L.base_factor(levers),
                    levers=levers, growth=L.growth(levers))
    if not plan["fits"]:
        relaxed = P.zones(record, relaxed=True, tighten=tighten)
        rfield = SizeField(surface, relaxed, growth=L.growth(levers), scale_to_m=1e-3)
        rhist = rfield.histogram(coarsest=coarsest, per_h3=P.PER_H3, stop_elements=STOP_ELEMENTS)
        plan = P.finish(record, zone_list, hist, level=level, diag_m=diag_m, base_factor=L.base_factor(levers),
                        relaxed_hist=rhist, levers=levers, growth=L.growth(levers))
    plan["mesh"] = L.mesh_settings(levers)
    plan["measure"] = {"faces": len(record.get("faces") or {}), "counts": record.get("counts") or {},
                       "thinnest_wall_mm": (record.get("thickness") or {}).get("min_mm"),
                       "bodies": record.get("bodies"), "notes": list(record.get("notes") or [])}
    return plan


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


def _refuse(ws: Path, *, cause: str, text: str, **facts) -> None:
    from meshpipeline.engines.preflight import PreflightRefusal

    PreflightRefusal(gate="fea_plan", cause=cause, builder_text=f"[FEA_PLAN] {text}",
                     facts={"engine": "gmsh", "before_meshing": True, "deterministic": True, **facts}).write(ws)


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


def _clear_outputs(ws: Path) -> None:
    for name in ("mesh.inp", "mesh.msh", "quality.json"):
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

    levers: list[dict] = []
    passes: list[dict] = []
    policy = get_spec(ENGINE).run_policy
    cap = int(policy.run_timeout()) if policy is not None else 1500
    for n in range(1, L.MAX_PASSES + 1):
        plan = await asyncio.to_thread(make_plan, record, surface, diag_m, fidelity=fidelity, levers=levers)
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
        await run.fence("start native mesh")
        result = await asyncio.to_thread(get_engine(ENGINE).run_cartesian_mesh, ws, timeout=cap)
        await run.fence("accept native mesh output")
        q = _read_quality(ws) if result.get("rc") in (0, 4) else {}
        made = bool(q) and (ws / "mesh.inp").exists()
        run.note_native_run(produced_usable_mesh=made and result.get("rc") == 0)
        passes.append(L.pass_facts(n, result.get("rc"), q, plan))
        if made:
            _keep_pass(ws, n)
        lever = L.next_lever(passes[-1], levers, plan)
        if lever is None:
            break
        _cite(job_id, [{"stage": "fea_ladder", "decided": "lever", "value": lever["lever"], "rule": lever["lever"],
                        "inputs": lever.get("inputs") or {}, "words": lever["words"]}])
        levers.append(lever)

    best, rule = L.best_pass(passes)
    _cite(job_id, [{"stage": "fea_ladder", "decided": "delivered pass", "value": best, "rule": "LAD-fea-5",
                    "inputs": {"passes": passes, "deciding": rule},
                    "words": (f"Pass {best} of {len(passes)} delivered." if best else "No pass built a valid mesh.")}])
    ok = best is not None
    if best is not None:
        _restore_pass(ws, best)
    (ws / "fea_passes.json").write_text(json.dumps({"passes": passes, "delivered": best, "levers": levers}, indent=1))
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


__all__ = ["BUILD_FAILED", "NOT_ONE_PART", "NO_SOURCE", "OVER_LEVEL", "applies", "drive", "driver_for",
           "make_plan", "measure_staged", "write_plan"]
