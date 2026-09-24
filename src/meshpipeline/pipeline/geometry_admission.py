# Responsibility: Decide whether the submitted geometry satisfies the chosen engine's input contract.
# Boundaries: it admits or rejects before any builder runs; it never repairs geometry.
from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import meshpipeline.agents.builder.settings as bcfg

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from meshpipeline.contracts.pipeline_state import PipelineState


async def _publish(job_id: str, level: str, text: str) -> None:
    from meshpipeline.contracts.event_stream import StaleExecutionPublish
    try:
        from meshpipeline.application.execution_publisher import execution_publisher
        _pub = execution_publisher(job_id, agent="geometry_admission")
        await _pub.astage(op_id="verdict")
        await _pub.anote(text, "error" if level == "ERROR" else "info", op_id="verdict")
        # the APPLICATION's conclusion about the input, beside its own message
        from meshpipeline.contracts import rationale as _rationale
        await _rationale.ageometry_admission(_pub, accepted=level != "ERROR", detail=text)
    except StaleExecutionPublish:
        raise
    except Exception:  # noqa: BLE001 - the user stream is never load-bearing
        pass


def _declared_evidence(state, engine: str):
    from meshpipeline.engines.admission import AdmissionEvidence, PatchSummary
    _patches = tuple(
        PatchSummary(name=str(p.get("name", "")).strip(), type=str(p.get("type", "")).strip())
        for p in (state.get("intake_patches") or []) if isinstance(p, dict))
    return AdmissionEvidence(
        engine=engine,
        purpose=state.get("purpose", "") or "",
        input_kind=state.get("input_kind", "") or None,
        dimensionality=state.get("dimensionality", "") or None,
        patches=_patches,
        engine_params=state.get("engine_params", {}) or {},
    )


# THE SLOT STAYS, AND IT READS.
#
# This node is the cheap second line, not the first. The measurement package refuses a bad file at
# step 2, seconds after the upload and before the customer has written a sentence, so nothing that
# gets this far should be a surprise. What the slot is for is the job that reached dispatch WITHOUT
# having been through step 2: an older session, a measurement that was switched off that week, a
# path someone adds later. Deleting it would remove the only check those jobs get.
#
# So it reads a stored verdict and computes nothing new. That is the whole change here, and it
# matters for a reason the plan's own summary got wrong: the slot is NOT a no-op today.
# `engines/vmtk/spec.py:190` declares `require_no_self_intersection=True` and vmtk is
# `implemented=True`, so a vmtk job stages a surface and probes it on this path right now. It is a
# no-op on the 322 corpus cases because they are all snappy and gmsh.

async def _stored_analysis(state, job_id: str) -> dict | None:
    """What the upload was measured to be, if anything. Never raises, never computes.

    None means nothing was stored and the caller falls through to the probe it has always run. A
    dict always carries `status`, so a rule never infers a verdict from an absent key.
    """
    try:
        from meshpipeline.cad.regions import reading_for_source
        from meshpipeline.pipeline.geometry_state import geometry_ref

        ref = geometry_ref(state)
        if ref is None:
            return None
        analysis = await reading_for_source(ref)
        if analysis is not None:
            logger.info("geometry_admission: read the stored measurement rather than probing - "
                        "job_id=%s status=%s", job_id, analysis.get("status"))
        return analysis
    except Exception as exc:                       # noqa: BLE001 - never block a build on a read
        logger.warning("geometry_admission: the stored measurement could not be read (%s) - "
                       "falling through to the probe - job_id=%s", exc, job_id)
        return None


async def node_geometry_admission(state: PipelineState) -> dict:
    import dataclasses

    from meshpipeline.cad.staging import prepare_surface
    from meshpipeline.cad.surface_checks import surface_analysis_for
    from meshpipeline.engines.registry import get_spec

    job_id = state.get("job_id", "unknown")
    engine = state.get("engine", "")
    spec = get_spec(engine)
    ic = spec.input_contract
    # Only engines whose contract MEASURES the geometry need this phase; everyone else proceeds
    # instantly (no staging, no probe) - this keeps the gate free for the wrap-then-fill engines.
    if ic is None or not (ic.require_no_self_intersection or ic.min_thickness_ratio > 0):
        return {}

    # READ FIRST. A measurement made at upload against these exact bytes answers the same question
    # the probe below answers, and it is already paid for. Only when there is none does this stage
    # a surface and measure one.
    analysis = await _stored_analysis(state, job_id)

    # Admission MEASURES the geometry, so it needs the same metre-normalised surface the builder
    # gets - an extent gate comparing millimetre numbers against metre thresholds would reject
    # sound geometry and admit unsound geometry, each by a factor of a thousand.
    from meshpipeline.pipeline.geometry_state import materialized as _materialized
    _geometry = _materialized(state)
    source_path = _geometry.path if _geometry else ""
    if analysis is None and (not source_path or not Path(source_path).exists()):
        # Nothing to inspect here (e.g. a programmatic submit without an upload path). The
        # builder's run_mesh gate remains the backstop, so never block on a missing file.
        return {}

    if analysis is None:
        with tempfile.TemporaryDirectory(prefix="admission-") as td:
            dest = Path(td) / "input.stl"
            try:
                _prepared = prepare_surface(_geometry, dest, engine=engine)
            except Exception as exc:  # a staging hiccup is a system issue - defer, never reject
                logger.warning("geometry_admission: could not stage surface for %s (%s) - "
                               "deferring to the builder - job_id=%s", engine, exc, job_id)
                return {}
            analysis = surface_analysis_for(spec, _prepared)

    if analysis is None:  # analysis unavailable/failed - defer to the builder, don't blame the user
        return {}
    from meshpipeline.contracts.geometry_measurement import STATUS_OK, STATUSES_ABOUT_THE_FILE
    if analysis.get("status") in STATUSES_ABOUT_THE_FILE:
        # A FACT ABOUT THE FILE, AND THE ONE MEASUREMENT OUTCOME THAT IS REFUSED HERE. The format
        # cannot be measured at all, so no retry and no other engine changes it: this part will be
        # meshed with nothing measured, nothing looked at, nothing asked and nothing planned. That is
        # the Surveyor absent, silently, which is the whole product missing - and it used to pass
        # through this branch as "a step of ours failed" and reach the builder without a word.
        # The measurement's own reason names what to send instead, so the refusal is an instruction.
        reason = str(analysis.get("reason") or "").strip() or (
            "this file's format cannot be measured, so nothing describes the part")
        logger.error("geometry_admission: the geometry's format cannot be measured - job_id=%s: %s",
                     job_id, reason)
        await _publish(job_id, "ERROR", f"Input rejected - {reason}")
        try:
            from meshpipeline.capture.logger import TrainingLogger
            TrainingLogger(job_id).log(
                "geometry_admission", op_id="admission:rejected", payload={
                    "engine": engine, "admitted": False, "codes": ["unsupported_format"],
                    "phases": ["measured"], "reason": reason, "surface_analysis": analysis})
        except Exception:  # noqa: BLE001 - the capture log is never load-bearing
            pass
        # The same shape a declared rejection returns: unfixable by retry, so exhaust the budget and
        # hand the reason to the executor short-circuit.
        return {
            "geometry_unsuitable_reason": reason,
            "executor_success":           False,
            "retry_count":                bcfg.MAX_BUILDER_RETRIES + 1,
        }
    if analysis.get("status") not in (None, STATUS_OK):
        # The measurement ran and could not finish, or declined for a reason of OUR deployment's. It
        # carries `status` and `reason` and no numbers, so there is nothing to judge: defer to the
        # builder exactly as an unavailable probe does, and never blame the customer for a step of
        # ours that failed.
        logger.info("geometry_admission: the stored measurement did not succeed (%s) - deferring "
                    "to the builder - job_id=%s", analysis.get("status"), job_id)
        return {}

    # The SAME EngineSpec.admit(), now with the full declared context PLUS the measured surface
    # (not a second gate - one method, more evidence). INVARIANT: before the builder loop starts,
    # the engine must have admitted the request on ALL evidence available so far - so EVERY
    # rejection is blocking, declared or measured. A declared rejection surfacing here means the
    # pipeline state drifted after intake (a data-contract bug); that must STOP the build, not warn.
    evidence = dataclasses.replace(_declared_evidence(state, engine), surface_analysis=analysis)
    rejections = spec.admit(evidence)
    if not rejections:
        # capture the ACCEPT with the measured evidence it was judged on (gap 4:
        # acceptance used to leave no record of what the admission actually saw)
        try:
            from meshpipeline.capture.logger import TrainingLogger
            TrainingLogger(job_id).log(
                "geometry_admission", op_id="admission:accepted",
                payload={"engine": engine, "admitted": True,
                         "surface_analysis": analysis})
        except Exception:
            pass
        return {}

    reason = "  ".join(r.message for r in rejections)
    _phases = ", ".join(f"{r.phase}:{r.code}" for r in rejections)
    logger.error("geometry_admission: input rejected for %s - job_id=%s [%s]: %s",
                 engine, job_id, _phases, reason)
    await _publish(job_id, "ERROR",
                   f"Input rejected - the engine cannot service this request: {reason}")
    try:
        from meshpipeline.capture.logger import TrainingLogger
        TrainingLogger(job_id).log(
            "geometry_admission", op_id="admission:rejected", payload={
            "engine": engine, "admitted": False,
            "codes": [r.code for r in rejections],
            "phases": [r.phase for r in rejections], "reason": reason,
            "rejections": [dataclasses.asdict(r) for r in rejections],
            "surface_analysis": analysis})
    except Exception:
        pass
    # Unfixable by any retry → exhaust the budget and hand the reason to the executor
    # short-circuit (executor_failed_gate='geometry' → FailureSection.GEOMETRY → gate_failed).
    return {
        "geometry_unsuitable_reason": reason,
        "executor_success":           False,
        "retry_count":                bcfg.MAX_BUILDER_RETRIES + 1,
    }
