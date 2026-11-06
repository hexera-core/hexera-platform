# Responsibility: Answer, for the run's live line only, "how many does this engine plan to make,
# and what are they called" - the engine's OWN estimate on record beside the case, and the engine's
# OWN word for one of them.
# Boundaries: asks the engine (EngineSpec.planned_cells_on_record, .counted_as) and nothing else.
# It infers, scales and guesses nothing: an engine that records no estimate gets none here, and the
# line then says only what the mesher has counted so far. A courtesy, never load-bearing: every
# failure returns (None, "") and no mesh is failed, delayed or altered by it.
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def planned_live_count(engine: str, workspace) -> tuple[int | None, str]:
    """`(of, unit)` for contracts/mesh_progress.publishing_sink: the cells or elements the case in
    `workspace` was planned at (None when this engine records no estimate) and the word a user
    hears for one of them ("" when the engine declares no delivered mesh). Never raises."""
    if not str(engine or "").strip():
        # NEVER THE DEFAULT ENGINE'S PLAN (registry.get_spec("") returns it): a run that cannot
        # say which engine it is gets no estimate and no noun, not another engine's.
        return None, ""
    try:
        from meshpipeline.engines.registry import get_spec
        spec = get_spec(str(engine))
        if spec is None:
            return None, ""
        return spec.planned_cells_on_record(workspace), spec.counted_as
    except Exception as exc:  # noqa: BLE001 - an estimate we cannot read is simply no estimate
        logger.debug("live estimate: none for %s (%s)", engine, type(exc).__name__)
        return None, ""


__all__ = ["planned_live_count"]
