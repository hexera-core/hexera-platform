# Responsibility: Build a snap-grid mesh for one Builder attempt with no model in the loop: stage the ECXML, choose the grid plan, run the mesher, report the outcome.
# Boundaries: deterministic; it authors nothing a model could get wrong. The mesher, the checks and the manifest are runner.py's and mesher.py's.
# Collaborates with: agents/builder/invoke.py (calls drive through spec.build_driver), engines/snapgrid/runner.py.
from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from meshpipeline.contracts.event_stream import StaleExecutionPublish

if TYPE_CHECKING:
    from meshpipeline.agents.builder.driver_run import BuilderDriverRun
    from meshpipeline.contracts.event_stream import ExecutionEventPublisher

logger = logging.getLogger(__name__)

#: Failure markers this driver ends an attempt with (the run record carries them).
NO_SOURCE = "snapgrid_source_missing"
BUILD_FAILED = "snapgrid_build_failed"


async def _say(publish, text: str, op_id: str, *, error: bool = False,
               warn: bool = False) -> None:
    if publish is None:
        return
    try:
        if error:
            await publish.aerror(text, op_id=op_id)
        elif warn:
            await publish.awarn(text, op_id=op_id)
        else:
            await publish.anote(text, op_id=op_id)
    except StaleExecutionPublish:
        raise
    except Exception:  # noqa: BLE001 - the user stream is never load-bearing
        pass


def thin_layer_warning(workspace) -> str:
    """What the user is told when the cell budget left a thin layer fewer cells through it than
    the plan asks for - the layers, and the budget that would keep them - or "" when none."""
    import json

    from meshpipeline.engines.snapgrid.runner import REPORT_NAME
    try:
        report = json.loads((Path(workspace) / REPORT_NAME).read_text())
    except (OSError, ValueError):
        return ""
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
        await _say(publish, "This run has no ECXML model to mesh - the snap-grid mesher reads the "
                            "ECXML file itself, so it cannot mesh a STEP or STL.",
                   "snapgrid:no-source", error=True)
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
    await _say(publish, f"Meshing the placed parts on a snap grid: up to {plan['max_cells']:,} "
                        f"cells, at least {max(1, plan['min_cells_across'])} cell(s) through every "
                        "layer, every part kept where the file puts it.", "snapgrid:start")
    policy = get_spec("snapgrid").run_policy
    cap = int(policy.run_timeout()) if policy is not None else 3600
    result = await asyncio.to_thread(get_engine("snapgrid").run_cartesian_mesh, ws, timeout=cap)
    # BEFORE the result is judged or announced: a superseded generation must not accept it
    await run.fence("accept native mesh output")
    ok = result.get("rc") == 0
    run.note_native_run(produced_usable_mesh=ok)
    if ok:
        try:
            from meshpipeline.contracts import rationale as _rationale
            await _rationale.abuilder_mesh_ready(publish, cells=result.get("cells"))
        except StaleExecutionPublish:
            raise
        except Exception:  # noqa: BLE001
            pass
        warning = thin_layer_warning(ws)
        if warning:
            await _say(publish, warning, "snapgrid:thin-layers", warn=True)
    else:
        why = _last_line(str(result.get("log_tail") or "")) or f"rc={result.get('rc')}"
        logger.warning("snapgrid: the mesher failed - job_id=%s rc=%s: %s", job_id,
                       result.get("rc"), why)
        await _say(publish, f"The snap-grid mesher did not deliver a mesh: {why}",
                   "snapgrid:failed", error=True)
    await run.fence("deliver builder outcome")
    outcome = run.outcome(produced_deliverable=ok, exhausted=not ok,
                          failure_marker="" if ok else BUILD_FAILED)
    return outcome.produced_deliverable, (outcome.terminal_value or BUILD_FAILED), outcome


__all__ = ["BUILD_FAILED", "NO_SOURCE", "drive", "thin_layer_warning"]
