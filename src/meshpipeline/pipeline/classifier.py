# Responsibility: Turn a failed run into the specific instruction the next builder attempt needs.
# Boundaries: deterministic classification; it re-runs nothing and decides no retry budget.
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from meshpipeline.capture.logger import TrainingLogger
from meshpipeline.pipeline.enums import SEAM_SECTIONS, FailureSection

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from meshpipeline.contracts.pipeline_state import PipelineState


def section_for_gate(engine: str, gate_key: str) -> str:
    if not gate_key:
        return FailureSection.MESH
    if gate_key in SEAM_SECTIONS:
        return SEAM_SECTIONS[gate_key]
    try:
        from meshpipeline.engines.registry import get_spec
        for g in get_spec(engine).gates:
            if g.key == gate_key:
                return g.section
    except Exception:  # noqa: BLE001 - an unknown engine must never break triage
        logger.warning("classifier: could not resolve gates for engine %r", engine)
    return FailureSection.MESH


def failed_axes(axis_findings: list | None) -> list[str]:
    if isinstance(axis_findings, list):
        return sorted(
            str(f.get("axis_key", "")) for f in axis_findings
            if isinstance(f, dict) and f.get("passed") is False and f.get("axis_key"))
    return []


async def node_classifier(state: PipelineState) -> dict:
    job_id      = state.get("job_id", "unknown")
    retry_count = state.get("retry_count", 0)
    engine      = state.get("engine", "")
    classifier_result: dict[str, Any]

    _caveats = state.get("requirement_caveats") or []
    if state.get("executor_success", False) and _caveats:
        # QUALITY passed; a stated requirement near-missed. The gate's own measured diagnostic
        # (already in executor_output) is the summary VERBATIM, so the builder retries for the
        # miss itself - never against a stale reviewer verdict from an earlier attempt. This
        # branch can only be reached with retries remaining (the route sends a spent ladder to
        # review instead), so freshness is structural: caveats are executor-owned per attempt.
        gate_key = "domain_extent"
        classifier_result = {
            "section":      section_for_gate(engine, gate_key),
            "summary":      state.get("executor_output", "") or "",
            "failed_gate":  gate_key,
            "failed_axes":  [],
            "error_source": "requirements_near_miss",
        }
        rebuild = False
        logger.info("Classifier: requirements NEAR-MISS (quality passed) - %d caveat(s) - "
                    "job_id=%s", len(_caveats), job_id)
    elif not state.get("executor_success", False):
 # executor rejected: the GATE names its section and authored the coaching
        gate_key = state.get("executor_failed_gate", "") or ""
        section  = section_for_gate(engine, gate_key)
        # The gate's own feedback IS the guidance - it speaks the engine's vocabulary and
        # already names the fix and what not to change. Re-summarising it only loses detail.
        summary  = state.get("executor_output", "") or ""
        # A deterministic gate cannot know that the whole APPROACH is wrong; it only knows
        # its own check failed. Rebuild is a reviewer judgement about the request.
        rebuild = False
        classifier_result = {
            "section":      section,
            "summary":      summary,
            "failed_gate":  gate_key,
            "failed_axes":  [],
            "error_source": "executor_fail",
        }
        logger.info("Classifier: executor FAIL - gate=%s section=%s - job_id=%s",
                    gate_key or "(none)", section, job_id)
    else:
 # reviewer rejected: it DECLARED which axes failed and whether to start over
        axes    = failed_axes(state.get("reviewer_axis_findings", []) or [])
        rebuild = bool(state.get("reviewer_rebuild_required", False))
        summary = state.get("reviewer_feedback", "") or state.get("reviewer_verdict", "")
        section = (FailureSection.TOPOLOGY if rebuild
                   else _section_for_axes(engine, state.get("purpose", ""), axes))
        classifier_result = {
            "section":      section,
            "summary":      summary,
            "failed_gate":  "",
            "failed_axes":  axes,
            "error_source": "reviewer_fail",
        }
        logger.info("Classifier: reviewer FAIL - axes=%s rebuild=%s section=%s - job_id=%s",
                    axes or "(none named)", rebuild, section, job_id)

    builder_mode = "rebuild" if rebuild else "retry"

    # one classification per builder retry; a replay of that retry is the same operation
    TrainingLogger(job_id).log("classifier_run", op_id=f"classifier:{retry_count}", payload={
        "section":       classifier_result["section"],
        "summary":       classifier_result["summary"],
        "failed_gate":   classifier_result["failed_gate"],
        "failed_axes":   classifier_result["failed_axes"],
        "error_source":  classifier_result["error_source"],
        "builder_mode":  builder_mode,
        "deterministic": True,
    }, attempt=retry_count)

    return {
        "retry_count":       retry_count,
        "classifier_result": classifier_result,
        "builder_mode":      builder_mode,
    }


# The ValidationAxis a review axis serves maps onto the attempt-log section. Both
# vocabularies are declared (engines.base.ValidationAxis / pipeline.enums.FailureSection);
# this is the only place they meet.
_VALIDATION_AXIS_TO_SECTION: dict[str, str] = {
    "integrity":   FailureSection.GEOMETRY,
    "quality":     FailureSection.MESH,
    "solvability": FailureSection.MESH,
    "conformance": FailureSection.GROUPS,
}


def _section_for_axes(engine: str, purpose: str, axes: list[str]) -> str:
    if not axes:
        return FailureSection.MESH
    try:
        from meshpipeline.engines.quality_criteria import compose_review_rubric
        by_name = {ax.name: ax for ax in compose_review_rubric(engine, purpose)}
        for name in axes:
            ax = by_name.get(name)
            if ax is not None:
                return _VALIDATION_AXIS_TO_SECTION.get(ax.validation_axis, FailureSection.MESH)
    except Exception:  # noqa: BLE001 - labelling must never break triage
        logger.warning("classifier: could not resolve review axes for engine %r", engine)
    return FailureSection.MESH
