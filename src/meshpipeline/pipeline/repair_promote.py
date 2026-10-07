# Responsibility: Let repaired geometry become the geometry a run meshes - but only once it has
#                 provably staged for the chosen engine, and never without recording what it replaced.
# Owns: the staging proof, the lineage record, and the refusal.
# Boundaries: it promotes bytes somebody else repaired and stored. It runs no repair, uploads
#             nothing, and decides no job's status.
# Collaborates with: cad/staging.py (the proof), contracts/geometry_source.py (the handles).
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


class PromotionRefused(RuntimeError):
    """The repaired geometry may not become the run's geometry.

    The run continues on the ORIGINAL, which is the honest outcome: we have not made the file
    better in any way the mesher can use, so pretending otherwise would hide a failed repair
    behind a mesh attempt that was always going to fail the same way.
    """


def derived_interpretation(original, *, source_id: str):
    """The original's physical meaning, carried onto the repaired source.

    REPAIR MUST NEVER REINTERPRET SCALE. A conservative repair closes gaps; it does not decide
    that a part drawn in millimetres was metres all along. So the unit, its factor and the basis
    on which they were resolved are carried across unchanged, and only the source the
    interpretation belongs to differs - which it must, because MaterializedGeometry refuses an
    interpretation that names another upload.
    """
    from meshpipeline.contracts.geometry_source import GeometryInterpretationRef

    return GeometryInterpretationRef(
        interpretation_id=original.interpretation.interpretation_id,
        geometry_source_id=str(source_id),
        unit=original.interpretation.unit,
        scale_to_metres=original.interpretation.scale_to_metres,
        basis=original.interpretation.basis,
        evidence=original.interpretation.evidence,
    )


def stages_for(geometry, *, engine: str) -> tuple[bool, str]:
    """Whether `geometry` can be staged for `engine`. The one proof promotion rests on.

    Staged into a throwaway directory and thrown away: this asks a question, it does not prepare
    the run. The builder stages again for real, from the geometry this call decided on.
    """
    from meshpipeline.cad.staging import prepare_surface

    with tempfile.TemporaryDirectory(prefix="repair-stage-") as td:
        try:
            prepare_surface(geometry, Path(td) / "input.stl", engine=engine)
        except Exception as exc:  # noqa: BLE001 - every failure here is "it did not stage"
            return False, f"{type(exc).__name__}: {exc}"
    return True, ""


def promote(state, *, repaired, engine: str = "", attempt: int = 0) -> dict:
    """Make `repaired` the run's geometry, or refuse. Returns the state update.

    WHY THIS REPLACES `geometry` RATHER THAN ADDING A SECOND FIELD. Every consumer in the pipeline
    reads one handle through `geometry_state.materialized()` - the admission gate, the staging
    boundary, the builder, the executor. A parallel `effective_geometry` would mean each of those
    choosing which one it meant, and the first reader to choose wrong would mesh different bytes
    than the gate inspected. So there is one geometry, it is the approved one, and what it
    REPLACED is recorded beside it as lineage rather than as a second candidate.

    The original is not lost: `repair_lineage` keeps its identity - source id, digest, size - and
    the upload it names is immutable and still in the catalogue, so the customer's bytes remain
    retrievable and provable after the run has moved on.
    """
    from meshpipeline.pipeline.geometry_state import materialized as _materialized

    original = _materialized(state)
    if original is None:
        raise PromotionRefused(
            "this run carries no geometry, so there is nothing for a repair to replace")
    if repaired is None:
        raise PromotionRefused("no repaired geometry was offered")

    # THE SAME BYTES ARE NOT A REPAIR. A repair that produced an identical digest changed nothing,
    # and promoting it would record a lineage step that did no work.
    if repaired.ref.sha256 == original.ref.sha256:
        raise PromotionRefused(
            "the repaired geometry is byte-identical to the original - nothing was repaired")

    # THE UNIT MAY NOT MOVE. Promotion carries the original's interpretation across, so a repaired
    # handle arriving with a different scale means somebody re-resolved the unit on the way - and
    # a part silently rescaled by a factor of a thousand is the worst failure this pipeline has.
    if (repaired.interpretation.unit != original.interpretation.unit
            or float(repaired.interpretation.scale_to_metres)
            != float(original.interpretation.scale_to_metres)):
        raise PromotionRefused(
            f"the repaired geometry claims {repaired.interpretation.unit!r} where the approved "
            f"unit is {original.interpretation.unit!r}; repair does not reinterpret scale")

    staged, why = stages_for(repaired, engine=engine)
    if not staged:
        # It did not stage, so it is not an improvement the mesher can use. The run stays on the
        # original and an operator decides what to do with the repair.
        raise PromotionRefused(
            f"the repaired geometry does not stage for {engine or 'the chosen engine'}: {why}")

    logger.info("repair_promote: repaired geometry promoted for engine=%s "
                "(original sha256=%s -> repaired sha256=%s) - job_id=%s",
                engine, original.ref.sha256[:12], repaired.ref.sha256[:12],
                state.get("job_id", "unknown"))

    return {
        "geometry": repaired.to_state(),
        # WHAT THIS RUN IS NO LONGER MESHING, and the proof it was replaced deliberately. Read by
        # the final result so a delivered mesh can say which bytes it was built from, and by the
        # operator console so a reviewer can fetch the original and compare.
        "repair_lineage": {
            "original": original.ref.identity(),
            "repaired": repaired.ref.identity(),
            "interpretation_id": original.interpretation.interpretation_id,
            "engine_staged_for": engine,
            "attempt": int(attempt),
        },
    }
