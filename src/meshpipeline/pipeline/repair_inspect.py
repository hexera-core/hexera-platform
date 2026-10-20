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
    import asyncio

    import meshpipeline.settings.cad_repair as rcfg
    from meshpipeline.cad.repair.contracts import RepairProfile, RepairStatus, RepairTarget
    from meshpipeline.pipeline.geometry_state import materialized as _materialized

    job_id = state.get("job_id", "unknown")
    geometry = _materialized(state)
    # A programmatic submit without an upload path, or a resumed process that has not
    # re-materialised yet: there is nothing on disk to inspect and nothing to say about it.
    if geometry is None or not geometry.path or not Path(geometry.path).exists():
        return {}

    source = Path(geometry.path)
    size = source.stat().st_size
    if size > rcfg.CAD_INSPECT_MAX_BYTES:
        # NOT INSPECTED, AND SAID SO. Reading a very large STEP costs seconds before anything is
        # measured, and a part this big is one an operator should look at anyway. Reporting it as
        # clean would be worse than reporting that we did not look.
        logger.info("node_repair_inspect: %d bytes is over the inspection size bound - not "
                    "inspected - job_id=%s", size, job_id)
        return {
            "repair_status": RepairStatus.inconclusive.value,
            "repair_report": {"service_failure": "not_inspected_too_large",
                              "size_bytes": size,
                              "limit_bytes": rcfg.CAD_INSPECT_MAX_BYTES},
        }

    def _inspect():
        # IMPORTED INSIDE THE GUARD. An inspection-only dependency that fails to import on a
        # worker holding sound geometry must not fail the mesh run: it is the same class of
        # event as the inspection itself raising, and gets the same inconclusive answer.
        from meshpipeline.cad.repair import inspect as _inspect_mod

        return _inspect_mod.inspect_geometry(
            geometry,
            profile=RepairProfile.conservative,
            target=RepairTarget.meshing,
            engine=state.get("engine", "") or "",
        )

    try:
        # OFF THE EVENT LOOP AND ON A CLOCK. The kernel has no await points and no interruption
        # point, so the work goes to a thread and is ABANDONED on expiry - the thread finishes in
        # the background while the run proceeds. Evidence must never spend a run's deadline.
        result = await asyncio.wait_for(asyncio.to_thread(_inspect),
                                        timeout=rcfg.CAD_INSPECT_TIMEOUT_S)
    except TimeoutError:
        logger.warning("node_repair_inspect: inspection exceeded %.1fs - continuing without it "
                       "- job_id=%s", rcfg.CAD_INSPECT_TIMEOUT_S, job_id)
        from meshpipeline.engines.determinism import note_time_limit
        note_time_limit("cad_inspect", "the inspection ran past its time limit: repair "
                        "status inconclusive", budget_s=float(rcfg.CAD_INSPECT_TIMEOUT_S))
        return {
            "repair_status": RepairStatus.inconclusive.value,
            "repair_report": {"service_failure": "inspection_timed_out",
                              "timeout_seconds": rcfg.CAD_INSPECT_TIMEOUT_S},
        }
    except Exception as exc:  # noqa: BLE001 - inspection never decides a run's outcome
        # THE DETAIL GOES TO THE OPERATOR LOG, NOT THE CUSTOMER'S REPORT. An exception's text can
        # carry a worker-local path (a vanished file raises FileNotFoundError naming it), and this
        # report is downloadable by the job's owner. The report carries a code; the log carries
        # the sentence.
        logger.warning("node_repair_inspect: inspection failed (%s: %s) - recording an "
                       "inconclusive report and continuing - job_id=%s",
                       type(exc).__name__, exc, job_id)
        return {
            "repair_status": RepairStatus.inconclusive.value,
            "repair_report": {"service_failure": "inspection_failed",
                              "error_type": type(exc).__name__},
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
