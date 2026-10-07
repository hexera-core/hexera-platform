# Responsibility: Repair the run's geometry by itself when the inspection can point at what is
#                 wrong, and hand the mesher the repaired file - or leave the run on the original.
# Owns: the decision to attempt, the attempt, and what it records about either.
# Boundaries: it repairs and promotes. It decides no job's status, delivers no artifact, and - like
#             every node before the admission gate - refuses nothing: a repair that cannot happen
#             leaves the run exactly as it was, for the gate to judge on the original.
# Collaborates with: cad/repair/triage.py (what to aim at), cad/repair/conservative.py (the bounded
#                    repair), pipeline/repair_promote.py (the staging proof and the lineage).
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import meshpipeline.settings.cad_repair as rcfg

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from meshpipeline.contracts.pipeline_state import PipelineState

# WHY THIS IS A NODE AND NOT AN OPERATOR TASK.
#
# A service that only tells a customer their file is broken has moved the work, not done it. The
# defects this acts on are the ones where the right answer is not a judgement: a wire with a gap, a
# curve that disagrees with its surface, an orientation that is backwards. Those have one correct
# repair, the kernel performs it, and asking a person to click a button first buys nothing.
#
# WHAT KEEPS IT HONEST is that every step can refuse and the refusal is always the same shape -
# carry on with the file the customer sent:
#
#   triage        may only recommend a repair for a defect it can POINT AT (cad/repair/triage.py).
#                 An invalid part nobody can localise, or one whose defects are questions about
#                 intent, goes to a person instead.
#   the repair    measures its own result and refuses it if it moved the geometry too far, inflated
#                 a tolerance, deleted a face or lost a solid (cad/repair/conservative.py).
#   promotion     refuses geometry that does not provably stage for the chosen engine, that is
#                 byte-identical, or that claims a different unit (pipeline/repair_promote.py).
#
# So the worst case is the behaviour of not having tried, and the original upload remains immutable
# and retrievable throughout. That is what makes doing this without asking defensible.

#: Formats the bounded repair can rewrite. An IGES file is read and written back as STEP, which
#: changes the suffix the builder stages from, so it is left for a later slice rather than
#: half-handled here.
_REPAIRABLE_SUFFIXES = {".step", ".stp"}


def _derived_source(original, repaired_path: Path):
    """A geometry handle for bytes WE produced, derived from the customer's upload.

    DERIVED EXECUTION GEOMETRY, NOT A CATALOGUED UPLOAD. These bytes are not a second thing the
    customer sent, so they get no row in `geometry_sources`: the source of record stays the
    immutable upload, and this handle exists for the run that is meshing the repair. Its digest is
    computed from the file on disk, because a handle claiming a digest it does not have would make
    every provenance statement built on it a lie.

    The id is deterministic in the repaired content, so re-deriving the same repair twice - a
    resumed run, a retried attempt - produces the same identity rather than a new one each time.
    """
    from meshpipeline.contracts.geometry_source import (
        GeometrySourceRef,
        MaterializedGeometry,
        sha256_of,
    )
    from meshpipeline.pipeline.repair_promote import derived_interpretation

    digest, size = sha256_of(repaired_path)
    source_id = f"{original.ref.source_id}:repaired:{digest[:16]}"
    return MaterializedGeometry(
        ref=GeometrySourceRef(
            source_id=source_id,
            owner_id=original.ref.owner_id,
            # Where the repaired bytes are DELIVERED from (application/repair_report_delivery.py
            # uploads them under this job). Nothing reads this to materialise - a resumed run
            # re-derives the repair from the original - so it names the delivered object rather
            # than inventing a source key that no catalogue entry backs.
            object_key=f"jobs/{original.ref.source_id}/repaired{repaired_path.suffix.lower()}",
            sha256=digest, size_bytes=size,
            original_filename=repaired_path.name,
            suffix_hint=repaired_path.suffix.lower()),
        interpretation=derived_interpretation(original, source_id=source_id),
        local_path=repaired_path)


def _declined(reason: str, *, route: str = "", detail: dict | None = None) -> dict:
    """Nothing was changed, and the run says why. Never an error: the gate judges the original."""
    record = {"attempted": False, "reason": reason, "route": route}
    if detail:
        record["detail"] = detail
    return {"repair_attempt": record}


async def node_repair_attempt(state: PipelineState) -> dict:
    """Repair the geometry if the evidence supports it, and hand on whichever file should be meshed."""
    import asyncio

    from meshpipeline.cad.repair.triage import ROUTE_CONSERVATIVE_REPAIR, recommend
    from meshpipeline.pipeline.geometry_state import materialized as _materialized

    job_id = state.get("job_id", "unknown")
    report = dict(state.get("repair_report") or {})
    if not report:
        return _declined("this run carries no inspection to act on")
    if not rcfg.CAD_REPAIR_ENABLED:
        return _declined("CAD repair is switched off in this deployment")
    if not rcfg.CAD_REPAIR_AUTONOMOUS:
        return _declined("automatic repair is switched off; the report stands for an operator")

    original = _materialized(state)
    if original is None or not original.path or not Path(original.path).exists():
        return _declined("there is no materialised geometry to repair")

    source = Path(original.path)
    if source.suffix.lower() not in _REPAIRABLE_SUFFIXES:
        return _declined(f"the bounded repair does not rewrite {source.suffix.lower()} files")

    engine = str(state.get("engine", "") or "")
    advice = recommend(repair_status=str(state.get("repair_status", "") or ""),
                       report=report, target_engine=engine)
    if advice.route != ROUTE_CONSERVATIVE_REPAIR:
        # Triage declined to recommend an automatic repair: abstained, or routed this to a person
        # or to the customer. Either way it is not ours to attempt.
        return _declined(f"triage routed this to {advice.route}", route=advice.route,
                         detail={"reasons": list(advice.reasons),
                                 "abstain_reason": advice.abstain_reason})

    # THE REPAIRED FILE LIVES IN THE JOB'S OWN WORKSPACE, beside the materialised original, so it
    # survives for the builder that stages it. A resumed run re-materialises the original into the
    # same place and re-derives the repair, which is why the derived identity is deterministic.
    destination = source.with_name(f"{source.stem}-repaired{source.suffix}")

    from meshpipeline.cad.repair.conservative import (
        RepairRefused,
        RepairUnavailable,
        repair_step_file,
        status_for,
    )
    try:
        # OFF THE EVENT LOOP. ShapeFix on a real part is seconds of CPU with no await points, and
        # blocking the loop stalls the heartbeat that keeps this worker's lease alive.
        repair_report = await asyncio.to_thread(repair_step_file, source, destination)
    except RepairUnavailable as exc:
        # OUR limitation, never a verdict on the file.
        return _declined(f"the repair could not run here: {exc}", route=advice.route)
    except RepairRefused as exc:
        # The kernel produced something and the caps declined it. The customer's file is unharmed
        # and this is the evidence an operator needs to decide what to do instead.
        logger.info("node_repair_attempt: repair refused (%s) - continuing on the original "
                    "- job_id=%s", exc, job_id)
        return _declined(f"the repair was refused: {exc}", route=advice.route,
                         detail={"measurements": exc.measurements})
    except Exception as exc:  # noqa: BLE001 - a repair that breaks is never the file's fault
        logger.warning("node_repair_attempt: the repair failed (%s) - continuing on the original "
                       "- job_id=%s", type(exc).__name__, job_id)
        return _declined(f"the repair failed: {type(exc).__name__}", route=advice.route)

    from meshpipeline.pipeline.repair_promote import PromotionRefused, promote
    try:
        repaired = _derived_source(original, destination)
        update = promote(state, repaired=repaired, engine=engine,
                         attempt=int(state.get("retry_count", 0) or 0))
    except PromotionRefused as exc:
        # It repaired, but the result is not something the mesher can use - so the run stays on the
        # original and the repair becomes evidence rather than geometry.
        logger.info("node_repair_attempt: promotion refused (%s) - continuing on the original "
                    "- job_id=%s", exc, job_id)
        return _declined(f"the repaired geometry was not promoted: {exc}", route=advice.route)

    operations = [op["name"] for op in repair_report.operations]
    measured = {m.name: m.value for m in repair_report.measurements}
    logger.info("node_repair_attempt: repaired and promoted for engine=%s - operations=%s "
                "- job_id=%s", engine, operations, job_id)
    try:
        from meshpipeline.capture.logger import TrainingLogger
        TrainingLogger(job_id).log("repair_attempt", op_id="repair:attempt", payload={
            "route": advice.route, "targets": [dict(t) for t in advice.targets],
            "operations": operations, "report": repair_report.to_dict(),
            "lineage": update.get("repair_lineage", {})})
    except Exception:  # noqa: BLE001 - the corpus is never load-bearing for a run
        pass

    return {
        **update,
        "repair_status": status_for(repair_report).value,
        "repair_attempt": {
            "attempted": True,
            "route": advice.route,
            "operations": operations,
            # WHAT THE REPAIR AIMED AT, kept with what it did: a reviewer approving this reads the
            # defects that justified it beside the operations that answered them.
            "targets": [dict(t) for t in advice.targets],
            "holes_filled": measured.get("holes_filled", []),
            "holes_left": measured.get("holes_left", []),
            "summary": repair_report.summary,
        },
    }
