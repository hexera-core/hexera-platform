# Responsibility: Build a snap-grid mesh for one Builder attempt with no model in the loop: stage the ECXML, choose the grid plan, run the mesher, report the outcome.
# Boundaries: deterministic; it authors nothing a model could get wrong. It says its plan and the mesher's start on the run's live stream, as every route does (contracts/narration, contracts/mesh_design); the rest of what the user reads comes through finalize's output and the manifest. The mesher, the checks and the manifest are runner.py's and mesher.py's.
# Collaborates with: agents/builder/invoke.py (calls drive through spec.build_driver), engines/snapgrid/runner.py.
from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from meshpipeline.agents.builder.driver_run import BuilderDriverRun
    from meshpipeline.contracts.event_stream import ExecutionEventPublisher

logger = logging.getLogger(__name__)

#: Failure markers this driver ends an attempt with (the run record carries them).
NO_SOURCE = "snapgrid_source_missing"
BUILD_FAILED = "snapgrid_build_failed"


def thin_layer_warning(report: dict) -> str:
    """What the user is told when the cell budget left a thin layer fewer cells through it than
    the plan asks for - the layers, and the budget that would keep them - or when a gap, heated
    surface, heat source or opening came out coarser than asked; "" when none."""
    short = report.get("thin_layers_short") or []
    rules = (report.get("resolution") or {}).get("short_lines") or []
    need_all = int(report.get("rules_budget") or 0)
    tail = ((" " if short else "") + "Coarser than asked: " + "; ".join(str(r) for r in rules)
            + "." + (f" A budget of at least {need_all:,} cells keeps every rule." if need_all
                     else "")) if rules else ""
    if not short:
        return tail
    asked = short[0].get("wanted")
    names = ", ".join(str(r.get("part")) for r in short[:5]) + (" and more" if len(short) > 5
                                                                 else "")
    need = int(report.get("layers_budget") or 0)
    return (f"{len(short)} thin layer(s) ({names}) have fewer than the {asked} cells through "
            "them this fidelity asks for, to fit the cell budget. No layer is lost - each keeps "
            "at least one cell."
            + (f" A budget of at least {need:,} cells keeps {asked} through every layer."
               if need else "") + tail)


def _last_line(text: str) -> str:
    return next((ln.strip() for ln in reversed((text or "").splitlines()) if ln.strip()), "")


async def drive(workspace, state, *, job_id: str, publish: ExecutionEventPublisher,
                run: BuilderDriverRun, source_path: str = ""):
    from meshpipeline.agents.builder.driver_run import (
        STOP_REVIEWED_CASE_REPEATS,
        review_caused_retry,
    )
    from meshpipeline.contracts.mesh_execution import note_native_pass, note_native_payload
    from meshpipeline.engines.runtime import get_engine
    from meshpipeline.engines.snapgrid import runner as R

    ws = Path(workspace)
    src = R.find_source(source_path)
    if src is None:
        logger.warning("snapgrid: no ECXML model beside %s - job_id=%s", source_path, job_id)
        await run.fence("deliver builder outcome")
        outcome = run.outcome(produced_deliverable=False, failure_marker=NO_SOURCE)
        return False, NO_SOURCE, outcome

    # A retry a review asked for is meshed one fidelity level finer: the build is deterministic,
    # so the same plan would rebuild exactly the mesh the review just rejected.
    step = 1 if review_caused_retry(state) else 0
    plan = R.plan_for(str(state.get("effective_mesh_fidelity", "") or ""), step)
    if plan is None:
        await run.fence("deliver builder outcome")
        outcome = run.outcome(produced_deliverable=False,
                              failure_marker=STOP_REVIEWED_CASE_REPEATS)
        return False, STOP_REVIEWED_CASE_REPEATS, outcome

    shutil.copy2(src, ws / R.SOURCE_NAME)
    R.write_plan(ws, plan)
    run.note_authoring()
    # what the remote mesher receives: the model and the plan, nothing a previous pass left
    note_native_pass(ws, 1)
    note_native_payload(ws, [R.SOURCE_NAME, R.PLAN_NAME])

    await run.fence("start native mesh")
    # THE DESIGN AND THE MESH START, said as every route says them: the plan's own numbers in plain
    # words, the design receipt (contracts/mesh_design), then the mesher starting - so the run box's
    # "Designing the mesh" and "Creating the mesh" rows fill for a thermal model as for any part
    from meshpipeline.contracts import mesh_design
    from meshpipeline.contracts import narration as N
    for i, line in enumerate(N.grid_plan_lines(plan)):
        await N.asay(publish, line, op_id=f"snapgrid:say:{step}:{i}")
    await mesh_design.apublish(publish, job_id=job_id, workspace=ws, attempt=step + 1)
    meshing = getattr(publish, "ameshing", None)
    if meshing is not None:
        await meshing("snapgrid", 0, None, op_id=f"meshing:snapgrid:{step}")
    # NO GUESSED TIME LIMIT (contracts/native_time): the run's cap is the WALL of the machine it is
    # on, and the mesher is watched for progress inside (runner.run_native_build); a run stopped at
    # the Cloud Run wall still working runs again, the same case, on a VM (RUN-5). It was 3,600 s.
    from meshpipeline.contracts.native_time import run_off_the_wall
    engine = get_engine("snapgrid")
    result = await asyncio.to_thread(
        run_off_the_wall, ws, lambda cap: engine.run_cartesian_mesh(ws, timeout=cap),
        engine="snapgrid", job_id=job_id)
    # BEFORE the result is judged: a superseded generation must not accept it
    await run.fence("accept native mesh output")
    ok = result.get("rc") == 0
    run.note_native_run(produced_usable_mesh=ok)
    if not ok:
        logger.warning("snapgrid: the mesher failed - job_id=%s rc=%s: %s", job_id,
                       result.get("rc"), _last_line(str(result.get("log_tail") or "")))
    await run.fence("deliver builder outcome")
    outcome = run.outcome(produced_deliverable=ok, exhausted=not ok,
                          failure_marker="" if ok else BUILD_FAILED)
    return outcome.produced_deliverable, (outcome.terminal_value or BUILD_FAILED), outcome


__all__ = ["BUILD_FAILED", "NO_SOURCE", "drive", "thin_layer_warning"]
