# Responsibility: Simplify the run's geometry by itself when inspection found removable features
#                 and a local simulation check says the answer barely moves, and hand the mesher the
#                 simplified file - or leave the run on the geometry it had.
# Owns: the decision to attempt, the attempt, and what it records about either.
# Boundaries: it simplifies and promotes. It decides no job's status, delivers no artifact, and -
#             like every node before the admission gate - refuses nothing: a simplification that
#             cannot happen leaves the run exactly as it was, for the gate to judge. The twin of
#             pipeline/repair_attempt.py.
# Collaborates with: cad/simplify/simplify.py (the bounded, verified defeaturing),
#                    pipeline/simplify_promote.py (the staging proof and the lineage).
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import meshpipeline.settings.cad_simplify as scfg

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from meshpipeline.contracts.pipeline_state import PipelineState

# WHAT KEEPS IT HONEST is what keeps the repair attempt honest: every step can refuse, and the
# refusal is always the same shape - carry on with the geometry the run already had:
#
#   the choice     only inspection's removable candidates, never a slot end, never a feature in the
#                  load case's end bands (cad/simplify/simplify.eligible).
#   T0             the defeatured part must be a valid closed solid that moved little material.
#   T2             each removed feature is judged by a local solve driven by one global solve of
#                  the simplified part, and kept only inside the error budget (cad/simplify/verify).
#   promotion      refuses geometry that does not stage for the chosen engine, that is byte-
#                  identical, or that claims a different unit (pipeline/simplify_promote.py).
#
# So the worst case is the behaviour of not having tried, and the upload stays immutable throughout.

#: Formats the simplification rewrites: it reads and writes STEP, as the bounded repair does.
_SIMPLIFIABLE_SUFFIXES = {".step", ".stp"}


def _declined(reason: str, *, outcome: str, status: str = "", detail: dict | None = None) -> dict:
    """Nothing was changed, and the run says why. Never an error: the gate judges the geometry.

    `outcome` is the same why as a closed word (off, no_report, no_candidates, no_geometry, format,
    timeout, unavailable, refused, failed, not_promoted, not_proved). `status` is "inconclusive"
    when OUR side gave no answer (a clock, a missing tool, an error) and "refused" when the check
    answered no - neither says anything about the part beyond that."""
    record = {"attempted": False, "reason": reason, "outcome": outcome}
    if status:
        record["status"] = status
    if detail:
        record["detail"] = detail
    return {"simplify_attempt": record}


def _derived_source(original, simplified_path: Path):
    """A geometry handle for bytes WE produced, derived from the geometry the run had.

    As repair_attempt._derived_source: no `geometry_sources` row, a digest computed from the file,
    an id deterministic in the content - `<source_id>:simplified:<sha16>`. A repaired part's
    source id is already `<upload>:repaired:<sha16>`, so the chain is readable from the id alone."""
    from meshpipeline.contracts.geometry_source import (
        GeometrySourceRef,
        MaterializedGeometry,
        sha256_of,
    )
    from meshpipeline.pipeline.simplify_promote import derived_interpretation

    digest, size = sha256_of(simplified_path)
    source_id = f"{original.ref.source_id}:simplified:{digest[:16]}"
    return MaterializedGeometry(
        ref=GeometrySourceRef(
            source_id=source_id,
            owner_id=original.ref.owner_id,
            # Nothing reads this to materialise - a resumed run re-derives the simplification from
            # the geometry it had - and no delivery of the simplified file exists yet.
            object_key=f"jobs/{original.ref.source_id}/simplified{simplified_path.suffix.lower()}",
            sha256=digest, size_bytes=size,
            original_filename=simplified_path.name,
            suffix_hint=simplified_path.suffix.lower()),
        interpretation=derived_interpretation(original, source_id=source_id),
        local_path=simplified_path)


def _log(job_id: str, payload: dict) -> None:
    try:
        from meshpipeline.capture.logger import TrainingLogger
        TrainingLogger(job_id).log("simplify_attempt", op_id="simplify:attempt", payload=payload)
    except Exception:  # noqa: BLE001 - the corpus is never load-bearing for a run
        pass


async def node_simplify_attempt(state: PipelineState) -> dict:
    """Simplify the geometry if the evidence supports it, and hand on whichever file should be meshed.

    OFF TOUCHES NOTHING: with CAD_SIMPLIFY_ENABLED off this records the decline and returns before
    any kernel import. simplify_step_file checks the same switch again before its first kernel call.
    """
    import asyncio

    from meshpipeline.pipeline.geometry_state import materialized as _materialized

    job_id = str(state.get("job_id", "unknown"))
    if not scfg.CAD_SIMPLIFY_ENABLED:
        return _declined("CAD simplification is switched off in this deployment", outcome="off")
    report = dict(state.get("simplify_report") or {})
    if not report:
        return _declined("this run carries no simplification inspection to act on", outcome="no_report")
    if str(state.get("simplify_status", "") or "") != "candidates":
        return _declined("inspection found no feature a simplification could remove",
                         outcome="no_candidates")

    original = _materialized(state)
    if original is None or not original.path or not Path(original.path).exists():
        return _declined("there is no materialised geometry to simplify", outcome="no_geometry")
    source = Path(original.path)
    if source.suffix.lower() not in _SIMPLIFIABLE_SUFFIXES:
        return _declined(f"simplification does not rewrite {source.suffix.lower()} files",
                         outcome="format")

    candidates = (report.get("report") or {}).get("candidates") or []
    feature_ids = [str(c.get("id")) for c in candidates if isinstance(c, dict) and c.get("removable")]
    engine = str(state.get("engine", "") or "")
    # beside the geometry it replaces, in the job's own workspace, so it survives for the builder
    destination = source.with_name(f"{source.stem}-simplified{source.suffix}")

    from meshpipeline.cad.simplify.simplify import (
        SimplifyRefused,
        SimplifyUnavailable,
        simplify_step_file,
    )
    try:
        # OFF THE EVENT LOOP AND ON A CLOCK, as the repair is: defeaturing and the verification
        # solves are seconds to minutes with no await points. The thread is abandoned on expiry
        # (the kernel has no interruption point); the solver children carry the same deadline.
        simplified_report = await asyncio.wait_for(
            asyncio.to_thread(simplify_step_file, source, destination, feature_ids=feature_ids),
            timeout=scfg.CAD_SIMPLIFY_TIMEOUT_S)
    except TimeoutError:
        logger.warning("node_simplify_attempt: the simplification exceeded %.1fs - continuing on "
                       "the geometry it had - job_id=%s", scfg.CAD_SIMPLIFY_TIMEOUT_S, job_id)
        from meshpipeline.contracts.work_limits import TIME_SIMPLIFY_CLOCK
        from meshpipeline.engines.determinism import note_time_limit
        note_time_limit("cad_simplify", "the simplification ran past its time limit: the geometry "
                        "it had is meshed", rule=TIME_SIMPLIFY_CLOCK,
                        budget_s=float(scfg.CAD_SIMPLIFY_TIMEOUT_S))
        return _declined(f"the simplification exceeded {scfg.CAD_SIMPLIFY_TIMEOUT_S:.0f}s",
                         outcome="timeout", status="inconclusive")
    except SimplifyUnavailable as exc:
        # OUR limitation, never a verdict on the file.
        return _declined(f"the simplification could not run here: {exc}", outcome="unavailable",
                         status="inconclusive")
    except SimplifyRefused as exc:
        logger.info("node_simplify_attempt: simplification refused (%s) - continuing on the "
                    "geometry it had - job_id=%s", exc, job_id)
        # A DECLINE IS STILL A TRAINING ROW: every feature's estimate and verdict travels in it.
        _log(job_id, {"outcome": "refused", "refused_because": str(exc),
                      "operations": [dict(op) for op in exc.measurements.get("operations", [])],
                      "measurements": dict(exc.measurements)})
        return _declined(f"the simplification was refused: {exc}", outcome="refused",
                         status="refused", detail={"measurements": exc.measurements})
    except Exception as exc:  # noqa: BLE001 - a simplification that breaks is never the file's fault
        logger.warning("node_simplify_attempt: the simplification failed (%s) - continuing on the "
                       "geometry it had - job_id=%s", type(exc).__name__, job_id)
        return _declined(f"the simplification failed: {type(exc).__name__}", outcome="failed",
                         status="inconclusive")

    measured = dict(simplified_report.diagnostics)
    verification = dict(measured.get("verification") or {})
    kept = tuple(str(c.id) for c in simplified_report.candidates)
    from meshpipeline.pipeline.simplify_promote import PromotionRefused, promote
    try:
        simplified = _derived_source(original, destination)
        try:
            update = await asyncio.wait_for(
                asyncio.to_thread(promote, state, simplified=simplified, engine=engine,
                                  attempt=int(state.get("retry_count", 0) or 0), features=kept,
                                  estimated_change=float(verification.get("estimated_change") or 0.0)),
                timeout=scfg.CAD_SIMPLIFY_TIMEOUT_S)
        except TimeoutError:
            # A PROOF THAT DID NOT FINISH IS NOT A REFUSAL (repair_attempt says why); the run still
            # stays on the geometry it had, because bytes we never proved may not be promoted.
            from meshpipeline.contracts.work_limits import TIME_SIMPLIFY_CLOCK
            from meshpipeline.engines.determinism import note_time_limit
            note_time_limit("cad_simplify_promote", "the staging proof ran past its time limit: "
                            "the geometry it had is meshed", rule=TIME_SIMPLIFY_CLOCK,
                            budget_s=float(scfg.CAD_SIMPLIFY_TIMEOUT_S))
            return _declined(f"the staging proof did not finish within {scfg.CAD_SIMPLIFY_TIMEOUT_S:.0f}s, "
                             "so the simplification was not proved", outcome="not_proved",
                             status="inconclusive")
    except PromotionRefused as exc:
        logger.info("node_simplify_attempt: promotion refused (%s) - job_id=%s", exc, job_id)
        _log(job_id, {"outcome": "not_promoted", "refused_because": str(exc),
                      "operations": [dict(op) for op in simplified_report.operations],
                      "measurements": measured})
        return _declined(f"the simplified geometry was not promoted: {exc}", outcome="not_promoted",
                         status="refused")
    except Exception as exc:  # noqa: BLE001
        logger.warning("node_simplify_attempt: the simplified file could not be adopted (%s) - "
                       "job_id=%s", type(exc).__name__, job_id)
        return _declined(f"the simplified file could not be adopted: {type(exc).__name__}",
                         outcome="not_promoted", status="inconclusive")

    operations = [dict(op) for op in simplified_report.operations]
    logger.info("node_simplify_attempt: simplified and promoted for engine=%s - kept=%d - "
                "estimated change=%s - job_id=%s", engine, len(kept),
                verification.get("estimated_change"), job_id)
    _log(job_id, {"outcome": "simplified", "operations": operations, "measurements": measured,
                  "report": simplified_report.to_dict(),
                  "lineage": update.get("simplify_lineage", {})})

    # `simplify_status` IS NOT TOUCHED: it is the inspection's verdict on the geometry as it
    # arrived, and `simplify_report` beside it is that inspection (as repair_status is kept).
    return {
        **update,
        "simplify_attempt": {
            "attempted": True,
            "status": "simplified",
            "outcome": "simplified",
            # what was removed and kept, what was removed and put back, and what was never tried
            "operations": operations,
            "features_kept": list(kept),
            # every feature's estimate, the driver solve, the budget and what the package spends
            "verification": verification,
            "t0": dict(measured.get("t0") or {}),
            "summary": simplified_report.summary,
        },
    }
