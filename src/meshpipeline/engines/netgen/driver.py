# Responsibility: Build a Netgen mesh for one Builder attempt with no model in the loop: read the staged boundary, plan it from the measured passage field, run the native ladder, report the outcome.
# Boundaries: deterministic; the plan is engines/netgen/plan.py's, the mesh the runner's. It publishes nothing itself - what the user reads comes through finalize and the manifest.
# Collaborates with: agents/builder/invoke.py (calls drive through EngineSpec.build_driver), engines/netgen/staging.py, engines/netgen/plan.py, engines/netgen/runner.py.
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from meshpipeline.agents.builder.driver_run import BuilderDriverRun
    from meshpipeline.contracts.event_stream import ExecutionEventPublisher

logger = logging.getLogger(__name__)

#: Failure markers this driver ends an attempt with (the run record carries them).
NOT_STAGED = "netgen_not_staged"
NO_FIELD = "netgen_passage_unreadable"
BUILD_FAILED = "netgen_build_failed"
#: what the remote mesher receives
PAYLOAD = ("fluid_boundary.stl", "netgen_spec.json", "netgen_size.txt", "system",
           "request.txt", "patches_contract.txt", "engine_params.json", "flow_topology",
           "input_kind", "purpose", "port_openings.json", "netgen_staging.json")


def plan_case(ws: Path, state) -> dict | None:
    """Measure the staged boundary and write the plan; None when the boundary cannot be read."""
    import meshpipeline.settings.policy as polcfg
    from meshpipeline.engines.netgen import plan as P
    from meshpipeline.engines.netgen.staging import read_staging, staged_patch_files, staged_triangles
    from meshpipeline.engines.passage import staged_passage_field
    ws = Path(ws)
    rec = read_staging(ws)
    if not rec:
        return None
    solids, tris = staged_triangles(ws)
    if not len(tris):
        return None
    files = staged_patch_files(ws, rec)
    srcs = {k: str(v) for k, v in files.items()}
    field = staged_passage_field(rec, srcs, "wall", state.get("intake_patches") or [],
                                 read_sharp_edges=True)
    if field is None:
        return None
    pts, faces, radius = field
    wall_name = str(rec.get("wall_name") or "wall")
    walls = sorted({wall_name, *[k for k in solids if k == "wall" or k == wall_name]})
    ports = [o for k, o in (rec.get("openings") or {}).items() if isinstance(o, dict)]
    diam = [2.0 * float(np.sqrt(float(o.get("flow_area") or o.get("area") or 0.0) / np.pi))
            for o in ports if float(o.get("flow_area") or o.get("area") or 0.0) > 0.0]
    wall_tris = np.concatenate([v for k, v in solids.items() if k in walls]) if walls else tris
    wall_area = float(0.5 * np.linalg.norm(np.cross(wall_tris[:, 1] - wall_tris[:, 0],
                                                    wall_tris[:, 2] - wall_tris[:, 0]), axis=1).sum())
    layers = P.requested_layers(ws, state.get("engine_params") or {})
    # CFDREQ-1: THE USER'S REQUIREMENTS from their notes (engines/cfd_requirements) - the one seam. None: the plan is
    # exactly the rules' own
    kw: dict = {"layers": layers}
    ask = None
    from meshpipeline.engines import mesh_requirements as MR
    if (ws / MR.FILE).exists():
        from meshpipeline.engines import cfd_requirements as CR
        ask = CR.Ask.of(ws, "netgen")
        if ask is not None:
            kw = CR.netgen_inputs(ask, layers=layers, port_diameters=diam, soft_budget=P.SOFT_BUDGET,
                                  cell_limit=int(polcfg.CELL_HARD_LIMIT))
    plan = P.make_plan(wall_points=pts, wall_faces=faces, radius=radius, port_diameters=diam,
                       volume=P.closed_volume(tris), wall_area=wall_area, walls=walls,
                       cell_limit=int(polcfg.CELL_HARD_LIMIT), **kw)
    if ask is not None:
        CR.netgen_planned(ask, plan)
        ask.write(ws)
        CR.say_sync(ws, ask.lines())
    return P.write_plan(ws, plan, pts)


async def drive(workspace, state, *, job_id: str, publish: ExecutionEventPublisher,
                run: BuilderDriverRun, source_path: str = ""):
    from meshpipeline.contracts.mesh_execution import note_native_pass, note_native_payload
    from meshpipeline.contracts.native_time import run_off_the_wall
    from meshpipeline.engines.netgen.plan import plan_words
    from meshpipeline.engines.netgen.staging import FLUID_BOUNDARY
    from meshpipeline.engines.runtime import get_engine

    ws = Path(workspace)
    if not (ws / FLUID_BOUNDARY).exists():
        logger.warning("netgen: no staged fluid boundary in %s - job_id=%s", ws, job_id)
        await run.fence("deliver builder outcome")
        outcome = run.outcome(produced_deliverable=False, failure_marker=NOT_STAGED)
        return False, NOT_STAGED, outcome
    plan = await asyncio.to_thread(plan_case, ws, state)
    if plan is None:
        await run.fence("deliver builder outcome")
        outcome = run.outcome(produced_deliverable=False, failure_marker=NO_FIELD)
        return False, NO_FIELD, outcome
    logger.info("%s job_id=%s", plan_words(plan), job_id)
    try:
        from meshpipeline.contracts import decision_record as dr
        dr.cite(job_id, stage="builder", decided="the Netgen plan", value=plan_words(plan),
                rule="NG-1..NG-5", inputs={k: plan.get(k) for k in (
                    "cells_across", "h_narrow_m", "maxh_m", "layers", "layer_stack_m",
                    "estimated_cells", "cell_limit")},
                alternatives=[r["name"] for r in plan.get("rungs") or []],
                words=plan_words(plan))
    except Exception:  # noqa: BLE001 - a record never changes the build
        logger.debug("netgen: decision record not kept", exc_info=True)
    run.note_authoring()
    note_native_pass(ws, 1)
    note_native_payload(ws, [m for m in PAYLOAD if (ws / m).exists()])

    await run.fence("start native mesh")
    engine = get_engine("netgen")
    result = await asyncio.to_thread(
        run_off_the_wall, ws, lambda cap: engine.run_cartesian_mesh(ws, timeout=cap),
        engine="netgen", job_id=job_id)
    await run.fence("accept native mesh output")
    ok = result.get("rc") == 0
    run.note_native_run(produced_usable_mesh=ok)
    if not ok:
        tail = str(result.get("log_tail") or "").strip().splitlines()
        logger.warning("netgen: the mesher failed - job_id=%s rc=%s: %s", job_id, result.get("rc"),
                       tail[-1] if tail else "")
    else:
        try:
            (ws / "netgen_outcome.json").write_text(json.dumps(result.get("netgen") or {}, default=str))
        except OSError:
            pass
        # CFDREQ-6: a mesh built on a thinner rung than planned says so on the user's layer rows
        if (ws / "cfd_requirements_plan.json").exists():
            from meshpipeline.engines.cfd_requirements import netgen_built
            netgen_built(ws, (result.get("netgen") or {}).get("built"))
    await run.fence("deliver builder outcome")
    outcome = run.outcome(produced_deliverable=ok, exhausted=not ok,
                          failure_marker="" if ok else BUILD_FAILED)
    return outcome.produced_deliverable, (outcome.terminal_value or BUILD_FAILED), outcome


__all__ = ["BUILD_FAILED", "NOT_STAGED", "NO_FIELD", "PAYLOAD", "drive", "plan_case"]
