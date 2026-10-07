# Responsibility: Record what CAD repair inspection says about the run's verified geometry.
# Boundaries: diagnostics only - it never repairs, stages, replaces or rejects geometry.
# Collaborates with: cad/repair (the inspection core) and geometry_admission, which is the only
#                    gate that may refuse an input.
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from meshpipeline.contracts.pipeline_state import PipelineState


async def node_repair_inspect(state: PipelineState) -> dict:
    """Inspect the verified geometry on the way into a mesh run and record the report.

    This node is EVIDENCE, not a gate. It runs before node_geometry_admission so that a run
    which the admission gate later refuses already carries a typed account of WHAT is wrong with
    the file, and so a run that meshes fine still contributes its inspection to the corpus. Two
    boundaries make it safe to sit in the hot path of every mesh job:

      - it returns no `geometry`, so the handle the admission gate and the builder read stays the
        one the upload verified - repair is a separate, operator-approved act;
      - an inspection that falls over is OUR failure, so the report says `inconclusive` and the
        run continues. Accusing a customer's file because our OCCT import raised would send them
        to fix geometry that was never broken.
    """
    from meshpipeline.cad.repair.contracts import RepairProfile, RepairStatus, RepairTarget
    from meshpipeline.pipeline.geometry_state import materialized as _materialized

    job_id = state.get("job_id", "unknown")
    geometry = _materialized(state)
    # A programmatic submit without an upload path, or a resumed process that has not
    # re-materialised yet: there is nothing on disk to inspect and nothing to say about it.
    if geometry is None or not geometry.path or not Path(geometry.path).exists():
        return {}

    from meshpipeline.cad.repair import inspect as _inspect_mod
    try:
        result = _inspect_mod.inspect_geometry(
            geometry,
            profile=RepairProfile.conservative,
            target=RepairTarget.meshing,
            engine=state.get("engine", "") or "",
        )
    except Exception as exc:  # noqa: BLE001 - inspection never decides a run's outcome
        logger.warning("node_repair_inspect: inspection failed (%s) - recording an inconclusive "
                       "report and continuing - job_id=%s", exc, job_id)
        return {
            "repair_status": RepairStatus.inconclusive.value,
            "repair_report": {"service_failure": f"{type(exc).__name__}: {exc}"[:600]},
        }

    payload = result.to_dict()
    logger.info("node_repair_inspect: status=%s defects=%d engine=%s - job_id=%s",
                result.status.value, len(result.report.defects), state.get("engine", ""), job_id)
    try:
        from meshpipeline.capture.logger import TrainingLogger
        TrainingLogger(job_id).log("repair_inspect", op_id="repair:inspect", payload=payload)
    except Exception:  # noqa: BLE001 - the corpus is never load-bearing for a run
        pass

    return {"repair_status": result.status.value, "repair_report": payload}
