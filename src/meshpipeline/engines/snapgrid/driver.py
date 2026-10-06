# Responsibility: Build a snap-grid mesh for one Builder attempt with no model in the loop: stage the ECXML, choose the grid plan, run the mesher, report the outcome.
# Boundaries: deterministic; it authors nothing a model could get wrong. It publishes nothing itself (every execution-owned publication needs a certified scenario): what the user reads comes through finalize's output and the manifest. The mesher, the checks and the manifest are runner.py's and mesher.py's.
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
    the plan asks for - the layers, and the budget that would keep them - or "" when none."""
    short = report.get("thin_layers_short") or []
    if not short:
        return ""
    asked = short[0].get("wanted")
    names = ", ".join(str(r.get("part")) for r in short[:5]) + (" and more" if len(short) > 5
                                                                 else "")
    need = int(report.get("layers_budget") or 0)
    return (f"{len(short)} thin layer(s) ({names}) have fewer than the {asked} cells through "
            "them this fidelity asks for, to fit the cell budget. No layer is lost - each keeps "
            "at least one cell."
            + (f" A budget of at least {need:,} cells keeps {asked} through every layer."
               if need else ""))


def _last_line(text: str) -> str:
    return next((ln.strip() for ln in reversed((text or "").splitlines()) if ln.strip()), "")


async def drive(workspace, state, *, job_id: str, publish: ExecutionEventPublisher,
                run: BuilderDriverRun, source_path: str = ""):
    from meshpipeline.agents.builder.driver_run import (
        STOP_REVIEWED_CASE_REPEATS,
        review_caused_retry,
    )
    from meshpipeline.contracts.mesh_execution import note_native_pass, note_native_payload
    from meshpipeline.engines.registry import get_spec
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
    policy = get_spec("snapgrid").run_policy
    cap = int(policy.run_timeout()) if policy is not None else 3600
    result = await asyncio.to_thread(get_engine("snapgrid").run_cartesian_mesh, ws, timeout=cap)
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
