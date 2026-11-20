# Responsibility: Let simplified geometry become the geometry a run meshes - but only once it has
#                 provably staged for the chosen engine, and never without recording what it replaced.
# Owns: the simplification lineage record and the refusal.
# Boundaries: it promotes bytes cad/simplify/simplify.py wrote and the simulation check accepted. It
#             simplifies nothing, uploads nothing, and decides no job's status. The twin of
#             pipeline/repair_promote.py, whose staging proof, interpretation carry-over and
#             PromotionRefused it reuses rather than copies.
# Collaborates with: pipeline/repair_promote.py (the proof), contracts/geometry_source.py (the handles).
from __future__ import annotations

import logging

from meshpipeline.pipeline.repair_promote import PromotionRefused, derived_interpretation, stages_for

logger = logging.getLogger(__name__)

__all__ = ["PromotionRefused", "derived_interpretation", "promote", "stages_for"]


def promote(state, *, simplified, engine: str = "", attempt: int = 0,
            features: tuple[str, ...] = (), estimated_change: float = 0.0) -> dict:
    """Make `simplified` the run's geometry, or refuse. Returns the state update.

    ONE GEOMETRY, as repair_promote.promote: `geometry` is replaced, and what it REPLACED is recorded
    beside it as `simplify_lineage`. When the run had already been repaired, the geometry replaced
    here is the repaired part, `repair_lineage` is left exactly as it was, and the run carries both:
    upload -> repaired -> simplified, each step with the identity of what it replaced.

    Refused, the run staying on the geometry it had, on the cases repair promotion refuses: no
    geometry, nothing offered, byte-identical bytes, a different unit, or a file that will not stage.
    """
    import meshpipeline.settings.cad_simplify as scfg
    from meshpipeline.pipeline.geometry_state import materialized as _materialized

    original = _materialized(state)
    if original is None:
        raise PromotionRefused(
            "this run carries no geometry, so there is nothing for a simplification to replace")
    if simplified is None:
        raise PromotionRefused("no simplified geometry was offered")

    # THE SAME BYTES ARE NOT A SIMPLIFICATION: a lineage step that removed nothing.
    if simplified.ref.sha256 == original.ref.sha256:
        raise PromotionRefused(
            "the simplified geometry is byte-identical to the original - nothing was removed")

    # THE UNIT MAY NOT MOVE. Defeaturing fills holes; it never decides a millimetre part was metres.
    if (simplified.interpretation.unit != original.interpretation.unit
            or float(simplified.interpretation.scale_to_metres)
            != float(original.interpretation.scale_to_metres)):
        raise PromotionRefused(
            f"the simplified geometry claims {simplified.interpretation.unit!r} where the approved "
            f"unit is {original.interpretation.unit!r}; simplification does not reinterpret scale")

    staged, why = stages_for(simplified, engine=engine, budget_s=scfg.CAD_SIMPLIFY_TIMEOUT_S)
    if not staged:
        raise PromotionRefused(
            f"the simplified geometry does not stage for {engine or 'the chosen engine'}: {why}")

    logger.info("simplify_promote: simplified geometry promoted for engine=%s "
                "(original sha256=%s -> simplified sha256=%s) - job_id=%s",
                engine, original.ref.sha256[:12], simplified.ref.sha256[:12],
                state.get("job_id", "unknown"))

    return {
        "geometry": simplified.to_state(),
        # WHAT THIS RUN IS NO LONGER MESHING, which features left it, and the error the
        # simulation check estimated they spend. Never touches `repair_lineage`.
        "simplify_lineage": {
            "original": original.ref.identity(),
            "simplified": simplified.ref.identity(),
            "interpretation_id": original.interpretation.interpretation_id,
            "engine_staged_for": engine,
            "attempt": int(attempt),
            "features": list(features),
            "estimated_change": float(estimated_change),
            # THE GEOMETRY IT REPLACED, materialised: after a repair that is the repaired file, which
            # is still delivered as the repaired CAD (application/pipeline_run.py) - `geometry`
            # now names the simplified bytes and must not be delivered under that name.
            "replaced_geometry": original.to_state(),
        },
    }
