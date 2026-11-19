# Responsibility: Record what CAD simplification inspection says about the geometry this run will mesh: the features a simplification could remove and what each costs.
# Boundaries: diagnostics only - it never simplifies, stages, replaces or rejects geometry. The twin of pipeline/repair_inspect.py, and as safe in the hot path for the same reasons.
# Collaborates with: cad/simplify (the inspection core), node_repair_attempt (runs first, so a repaired part is the one inspected), geometry_admission (the only gate that may refuse an input).
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from meshpipeline.contracts.pipeline_state import PipelineState


async def node_simplify_inspect(state: PipelineState) -> dict:
    """Inspect the geometry on its way to admission and record the simplification report.

    EVIDENCE, NOT A GATE, exactly as node_repair_inspect: it returns no `geometry`, and an
    inspection that falls over is our failure, recorded as `inconclusive` while the run continues.
    It runs after node_repair_attempt, so when repair replaced the upload it is the repaired part
    that is inspected - the one the mesher will actually see.
    """
    import asyncio

    import meshpipeline.settings.cad_simplify as scfg
    from meshpipeline.cad.simplify.contracts import SimplifyStatus, SimplifyTarget
    from meshpipeline.pipeline.geometry_state import materialized as _materialized

    job_id = state.get("job_id", "unknown")
    geometry = _materialized(state)
    if geometry is None or not geometry.path or not Path(geometry.path).exists():
        return {}

    size = Path(geometry.path).stat().st_size
    if size > scfg.CAD_SIMPLIFY_INSPECT_MAX_BYTES:
        logger.info("node_simplify_inspect: %d bytes is over the inspection size bound - not "
                    "inspected - job_id=%s", size, job_id)
        return {
            "simplify_status": SimplifyStatus.inconclusive.value,
            "simplify_report": {"service_failure": "not_inspected_too_large", "size_bytes": size,
                                "limit_bytes": scfg.CAD_SIMPLIFY_INSPECT_MAX_BYTES},
        }

    def _inspect():
        # imported inside the guard: a dependency that fails to import must not fail the mesh run
        from meshpipeline.cad.simplify import inspect as _inspect_mod

        return _inspect_mod.inspect_features(geometry, target=SimplifyTarget.meshing,
                                             engine=state.get("engine", "") or "")

    try:
        result = await asyncio.wait_for(asyncio.to_thread(_inspect),
                                        timeout=scfg.CAD_SIMPLIFY_INSPECT_TIMEOUT_S)
    except TimeoutError:
        logger.warning("node_simplify_inspect: inspection exceeded %.1fs - continuing without it "
                       "- job_id=%s", scfg.CAD_SIMPLIFY_INSPECT_TIMEOUT_S, job_id)
        from meshpipeline.engines.determinism import note_time_limit
        note_time_limit("cad_simplify_inspect", "the simplification inspection ran past its time "
                        "limit: simplify status inconclusive",
                        budget_s=float(scfg.CAD_SIMPLIFY_INSPECT_TIMEOUT_S))
        return {
            "simplify_status": SimplifyStatus.inconclusive.value,
            "simplify_report": {"service_failure": "inspection_timed_out",
                                "timeout_seconds": scfg.CAD_SIMPLIFY_INSPECT_TIMEOUT_S},
        }
    except Exception as exc:  # noqa: BLE001 - inspection never decides a run's outcome
        # the detail goes to the operator log, not the downloadable report (it can carry a path)
        logger.warning("node_simplify_inspect: inspection failed (%s: %s) - recording an "
                       "inconclusive report and continuing - job_id=%s",
                       type(exc).__name__, exc, job_id)
        return {
            "simplify_status": SimplifyStatus.inconclusive.value,
            "simplify_report": {"service_failure": "inspection_failed",
                                "error_type": type(exc).__name__},
        }

    payload = result.to_dict()
    logger.info("node_simplify_inspect: status=%s candidates=%d engine=%s - job_id=%s",
                result.status.value, len(result.report.candidates), state.get("engine", ""), job_id)
    try:
        from meshpipeline.capture.logger import TrainingLogger
        TrainingLogger(job_id).log("simplify_inspect", op_id="simplify:inspect", payload=payload)
    except Exception:  # noqa: BLE001 - the corpus is never load-bearing for a run
        pass

    return {"simplify_status": result.status.value, "simplify_report": payload}
