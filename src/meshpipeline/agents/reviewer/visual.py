# Responsibility: Run the reviewer node: inspect the mesh and return a verdict with its evidence.
# Boundaries: it judges an ALREADY-VALIDATED mesh - the executor's gates ran first.
# Collaborates with: agents/reviewer/unified.py, render_runtime.py and sandbox/.
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import meshpipeline.agents.reviewer.settings as rcfg
from meshpipeline.agents.reviewer.context import build_review_prompt
from meshpipeline.agents.reviewer.deterministic_evidence import collect_deterministic_evidence
from meshpipeline.agents.reviewer.eligibility import (
    deterministic_evidence_complete,
    missing_target_obligations,
    validate_plan,
)
from meshpipeline.agents.reviewer.interaction_inputs import VisualReviewInteractionInputs
from meshpipeline.agents.reviewer.persist import save_review_artifacts
from meshpipeline.agents.reviewer.render_runtime import open_runtime
from meshpipeline.agents.reviewer.unified import UnifiedReviewOutcome, run_unified_review
from meshpipeline.application.execution_publisher import execution_publisher
from meshpipeline.contracts import human_flags as HF
from meshpipeline.contracts import model_inference as llm_router
from meshpipeline.contracts.event_stream import (
    ExecutionEventPublisher,
    StaleExecutionPublish,
)
from meshpipeline.contracts.evidence_ledger import EvidenceLedger
from meshpipeline.contracts.mesh_units import completed_mesh_unit
from meshpipeline.contracts.review_evidence import (
    RenderContext,
    ReviewEvidenceFailure,
    ReviewRenderError,
)
from meshpipeline.engines.assurance import derive_assurance_plan

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from meshpipeline.contracts.pipeline_state import PipelineState


def _recover_manifest(manifest: dict, workspace: Path, job_id: str) -> dict:
    if manifest and (manifest.get("mesh_paths") or {}).get("surface"):
        return manifest
    disk = workspace / "mesh_manifest.json"
    try:
        if disk.exists():
            loaded = json.loads(disk.read_text(encoding="utf-8"))
            if loaded:
                logger.info("Reviewer: recovered manifest from disk %s - job_id=%s", disk, job_id)
                return loaded
    except Exception as exc:  # noqa: BLE001
        logger.warning("Reviewer: could not read on-disk manifest %s - job_id=%s: %s",
                       disk, job_id, exc)
    return manifest


def _source_basename(state) -> str:
    from meshpipeline.pipeline.geometry_state import geometry_ref
    ref = geometry_ref(state)
    return os.path.basename(ref.original_filename) if ref and ref.original_filename else "unknown"


async def node_reviewer(state: PipelineState) -> dict:
    job_id    = state.get("job_id", "unknown")
    workspace = Path(state.get("openfoam_workspace", ""))
    manifest  = _recover_manifest(state.get("mesh_manifest", {}), workspace, job_id)

    from meshpipeline.engines.registry import get_spec
    engine        = state.get("engine", "")
    spec          = get_spec(engine)
    purpose       = state.get("purpose", "")
    engine_params = state.get("engine_params", {}) or {}
    retry_count   = state.get("retry_count", 0)
    review_save_dir = workspace / f"review_{retry_count + 1}"
    logger.info("Reviewer: starting - job_id=%s engine=%s attempt=%d", job_id, engine, retry_count)

    # THE reviewer's execution publisher. The review runs inside the graph, under the
    # claim taken before it started, so every event it publishes is ownership-checked.
    _publish = execution_publisher(job_id, agent="reviewer")
    await _publish.astage(op_id=f"inspect:{retry_count}")
    await _publish.anote("Inspecting the mesh against your brief", op_id=f"inspect:{retry_count}")

    def _read_txt(filename: str) -> str:
        p = workspace / filename
        try:
            return p.read_text(encoding="utf-8").strip() if p.exists() else ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("Reviewer: could not read %s - job_id=%s: %s", filename, job_id, exc)
            return ""

 # one plan, one obligation set
    # THE PHASE IS THE RETRY COUNTER'S MEANING, not a new flag: a dispute's first review runs
    # before any rebuild (retry_count 0) and every later one judges a rebuilt artifact.
    _dispute = state.get("user_dispute") or None
    _phase = ""
    if _dispute:
        _phase = HF.PHASE_PARENT if retry_count == 0 else HF.PHASE_REBUILT
    plan = derive_assurance_plan(spec, purpose, _dispute, _phase)

    # defense-in-depth, CHECKED FIRST. Only ever judge an ALREADY-VALIDATED mesh.
    # This moved above the inputs below because those now read the artifact's unit, and an
    # unvalidated execution has no completed mesh to read a unit FROM. Demanding one here would
    # turn "the mesh was never validated" - which has its own precise, user-facing non-verdict -
    # into an integrity error about a manifest field, blaming the artifact for not existing.
    if state.get("executor_success") is not True:
        return await _early_nonverdict(
            job_id=job_id, publish=_publish, review_save_dir=review_save_dir,
            manifest=manifest, retry_count=retry_count,
            marker="reviewer_evidence_missing",
            note="The mesh could not be verified because its execution was not validated. "
                 "This is a problem on our side - please try again.",
            chain="[EXECUTOR_SUCCESS_ABSENT] reviewer reached without validated execution")

    inputs = VisualReviewInteractionInputs(
        job_id=job_id,
        step_basename=_source_basename(state),
        retry_count=retry_count,
        workspace=workspace,
        manifest=manifest,
        review_save_dir=review_save_dir,
        mesh_units=completed_mesh_unit(manifest).value,
        review_brief=_read_txt("review_brief.txt") or state.get("review_brief_txt", ""),
        request=_read_txt("request.txt") or state.get("request_txt", ""),
        axis_names=list(plan.axis_names),
        publish=_publish,
        engine=engine,
        purpose=purpose,
        user_id=state.get("user_id", ""),
        user_dispute=_dispute,
        dispute_phase=_phase,
        prior_flag_findings=HF.findings_from_state(state.get("dispute_flag_findings")),
        builder_flag_responses=HF.responses_from_state(state.get("builder_flag_responses")),
        prior_reviewer_feedback=str(state.get("reviewer_feedback") or ""),
    )

    ok, problems = validate_plan(spec, plan)
    if not ok:
        logger.error("Reviewer: malformed assurance plan - job_id=%s: %s", job_id, problems)
        return await _nonverdict(inputs, "reviewer_evidence_missing",
                           "The review could not run because the assurance plan was malformed. "
                           "This is a problem on our side - please try again.",
                           f"[PLAN_INVALID] {problems}")

 # engine-owned target obligations (from trusted job data, NOT discovery)
    obligations = spec.expected_target_obligations(manifest, engine_params, purpose)

 # deterministic evidence, before any provider call
    ledger = EvidenceLedger()
    collect_deterministic_evidence(
        plan, ledger,
        measurements=(manifest.get("quality") or {}),
        criteria={c.key: c for c in spec.criteria},
        # executor_success (verified above) attests every blocking gate passed → file each as an
        # EXPLICIT pass; collect no longer defaults an absent gate to pass.
        gate_status=dict.fromkeys(plan.required_gate_keys, "pass"))
    complete, missing = deterministic_evidence_complete(plan, ledger)
    if not complete:
        logger.error("Reviewer: required deterministic evidence missing %s - job_id=%s",
                     missing, job_id)
        return await _nonverdict(inputs, "reviewer_evidence_missing",
                           "Required mesh-quality evidence was missing, so the mesh could not be "
                           "verified. This is a problem on our side - please try again.",
                           f"[EVIDENCE_MISSING] deterministic evidence: {list(missing)}")

 # open the engine's own renderer; one interactive review
    ctx = RenderContext(workspace=str(workspace), save_dir=str(review_save_dir), manifest=manifest)
    try:
        async with open_runtime(spec, ctx) as runtime:
            opening = await runtime.initial_context()
            if not opening.has_geometry:
                raise ReviewRenderError(ReviewEvidenceFailure.EVIDENCE_MISSING,
                                        "mesh loaded no renderable geometry (0 surface points)")
            # The reviewer's own inspection render. Safe mode publishes NOTHING for
            # these - the reader is told an image was produced (as tool activity), not
            # handed the image - so the bytes never enter the public backlog at all.
            # Raw mode publishes it, sanitized, through the same event.
            await _publish_inspection_image(_publish, opening.initial_screenshot_b64,
                                      op_id=f"opening-render:{retry_count}")

            # EXPECTED-TARGET-MISSING pre-check: an engine-RESOLVED obligation that discovery cannot
            # satisfy - whole-kind absence OR partial loss (3 groups expected, 1 discovered) - is
            # missing evidence, never waived because the inventory came up short.
            discovered_ids: dict = {}
            for t in opening.inspection_targets:
                discovered_ids.setdefault(t.kind, set()).add(t.target_id)
            absent = missing_target_obligations(obligations, discovered_ids)
            if absent:
                logger.error("Reviewer: expected targets not produced %s - job_id=%s",
                             absent, job_id)
                return await _nonverdict(inputs, "reviewer_evidence_missing",
                                   "The mesh did not expose review targets this engine expects, so "
                                   "it could not be verified. This is a problem on our side - please "
                                   "try again.",
                                   f"[EXPECTED_TARGET_MISSING] {absent}")

            system_prompt, review_context = build_review_prompt(
                manifest=manifest,
                nav_context=opening.nav_context,
                workspace=workspace,
                step_basename=inputs.step_basename,
                patch_names=list((manifest.get("patches") or {}).keys()),
                patch_colour_legend=opening.patch_colour_legend,
                patch_views=opening.patch_views,
                mesh_units=inputs.mesh_units,
                request=inputs.request,
                review_brief=inputs.review_brief,
                job_id=job_id,
                engine=engine,
                purpose=purpose,
                user_dispute=inputs.user_dispute,
                dispute_phase=inputs.dispute_phase,
                prior_flag_findings=inputs.prior_flag_findings,
                builder_flag_responses=inputs.builder_flag_responses,
                prior_reviewer_feedback=inputs.prior_reviewer_feedback,
            )
            outcome = await run_unified_review(
                plan=plan, ledger=ledger, runtime=runtime, opening=opening,
                system_prompt=system_prompt, opening_context_text=review_context,
                provider_call=llm_router.call_reviewer_with_tools,
                max_rounds=rcfg.REVIEWER_MAX_ROUNDS, # budget, -capped at pipeline in-loop
                total_timeout_s=rcfg.REVIEWER_TOTAL_TIMEOUT_SECONDS,
                pipeline_deadline_epoch=state.get("pipeline_deadline_epoch"),
                job_id=job_id, user_id=inputs.user_id, publish=_publish, obligations=obligations,
                attempt=inputs.retry_count,
                user_dispute=inputs.user_dispute, dispute_phase=inputs.dispute_phase)
    except ReviewRenderError as exc:
        return await _render_failure_result(inputs, exc)

    return await _translate_outcome(inputs, outcome, plan, ledger)


# THE EXACT set of state keys node_reviewer is permitted to return. The Reviewer INTERPRETS
# executed evidence; it owns none of the downstream truth. Anything outside this set - execution
# success/gate results (executor_success, executor_failed_gate), approved intent (engine, purpose,
# input_kind, dimensionality, mesh_fidelity, intake_patches, approved-snapshot fields), triage
# (classifier_result, builder_mode, retry_count), artifact readiness, job status, final_result,
# terminal message - is written by its legitimate owner (node_executor, node_classifier, the
# application terminal chain), never by the Reviewer. `_reviewer_return` is the SOLE construction site
# for the node's return dict and filters to this allow-list at RUNTIME, so a future edit that adds an
# unauthorized key cannot silently grant the Reviewer authority it must not have. (Defined here, after
# node_reviewer, purely so the node's line span - pinned by test_dependency_census - does not shift;
# module-level names resolve at call time, so position is immaterial to behaviour.)
_REVIEWER_RETURN_KEYS = frozenset({
    "reviewer_result", "reviewer_verdict", "reviewer_feedback", "reviewer_axis_findings",
    "reviewer_rebuild_required", "reviewer_tool_calls", "api_failure",
    # The per-flag BASELINE the parent-mesh review establishes. The Reviewer owns it because the
    # Reviewer is what measured it; the post-rebuild review only reads it.
    "dispute_flag_findings",
    # v8: the canonical accountability record for THIS invocation. Append-only history - the
    # Reviewer writes its own and never reads a previous attempt's.
    "agent_run_records",
})


def _reviewer_return(**fields) -> dict:
    _bad = set(fields) - _REVIEWER_RETURN_KEYS
    if _bad:
        raise AssertionError(
            f"node_reviewer attempted to write state key(s) it does not own: {sorted(_bad)}. "
            f"The Reviewer write surface is exactly {sorted(_REVIEWER_RETURN_KEYS)} - execution "
            "truth, approved intent, triage, artifact readiness and final_result belong to other "
            "owners.")
    return dict(fields)


def _findings_dump(plan, ledger, outcome: UnifiedReviewOutcome, attempt: int) -> list[dict]:
    _owner = {ax.name: getattr(ax, "owner", "") for ax in plan.axes}
    return [{
        "axis_key":     f.axis_key,
        "owner":        _owner.get(f.axis_key, ""),
        "passed":       f.passed,
        "finding":      f.finding,
        "evidence_ids": list(f.evidence_ids),
        "attempt":      attempt,
    } for f in outcome.findings]


async def _translate_outcome(inputs: VisualReviewInteractionInputs,
                             outcome: UnifiedReviewOutcome,
                       plan, ledger) -> dict:
    if outcome.api_failure:
        # A non-verdict from inside the interaction (provider failure, evidence-incomplete,
        # eligibility non-convergence, no-progress, exhaustion). The failure class is truthful;
        # only a real provider error is provider-down.
        result = await _nonverdict(inputs, outcome.api_failure,
                             _nonverdict_note(outcome.api_failure),
                             f"[{(outcome.failure_class or 'non_verdict').upper()}] {outcome.api_failure}",
                             tool_calls=outcome.tool_calls, messages=list(outcome.messages))
        if outcome.run_record:
            result["agent_run_records"] = [outcome.run_record]
        return result

    verdict   = outcome.verdict
    reasoning = outcome.reasoning
    findings  = _findings_dump(plan, ledger, outcome, inputs.retry_count)

    save_review_artifacts(
        inputs.review_save_dir, list(outcome.messages),
        {"verdict": verdict, "reasoning": reasoning, "axis_findings": findings,
         "rebuild_required": outcome.rebuild_required},
        inputs.manifest, tool_call_count=outcome.tool_calls,
        retry_count=inputs.retry_count, job_id=inputs.job_id)

    await inputs.publish.averdict(verdict)
    logger.info("Reviewer: verdict=%s tool_calls=%d - job_id=%s",
                verdict, outcome.tool_calls, inputs.job_id)
    return _reviewer_return(
        agent_run_records=[outcome.run_record] if outcome.run_record else [],
        reviewer_result=f"{verdict}\n{reasoning}",
        reviewer_verdict=verdict,
        reviewer_feedback=reasoning if verdict == "FAIL" else "",
        reviewer_axis_findings=findings,
        reviewer_rebuild_required=outcome.rebuild_required,
        reviewer_tool_calls=[outcome.tool_calls],
        # The baseline is written by the phase that establishes it and never overwritten by the
        # phase that is judged against it - otherwise the comparison would be with itself.
        **({"dispute_flag_findings": HF.as_dicts(outcome.flag_findings)}
           if inputs.dispute_phase == HF.PHASE_PARENT and outcome.flag_findings else {}),
    )


def _nonverdict_note(marker: str) -> str:
    if marker in ("reviewer_render_unavailable",):
        return ("Visual verification could not be completed because the mesh rendering step was "
                "unavailable. This is a problem on our side - please try again.")
    if _is_provider_failure(marker):
        return ("The review service is temporarily unavailable. This is a problem on our side - "
                "please try again.")
    return ("The mesh could not be fully verified from the available evidence, so it was not "
            "accepted. This is a problem on our side - please try again.")


def _is_provider_failure(marker: str) -> bool:
    return marker not in {
        "reviewer_render_unavailable", "reviewer_evidence_missing", "reviewer_exhausted",
    }


@dataclass(frozen=True)
class _EarlyRefusalInputs:

    job_id: str
    publish: ExecutionEventPublisher
    review_save_dir: Path
    manifest: dict
    retry_count: int


async def _early_nonverdict(*, job_id: str, publish: ExecutionEventPublisher,
                            review_save_dir: Path, manifest: dict,
                      retry_count: int, marker: str, note: str, chain: str) -> dict:
    return await _nonverdict(
        _EarlyRefusalInputs(job_id=job_id, publish=publish, review_save_dir=review_save_dir,
                            manifest=manifest, retry_count=retry_count),
        marker, note, chain)


async def _nonverdict(inputs: VisualReviewInteractionInputs | _EarlyRefusalInputs, marker: str,
                      note: str, chain: str,
                *, tool_calls: int = 0, messages: list | None = None) -> dict:
    # the marker names WHICH non-verdict this is; the attempt separates genuine retries
    await inputs.publish.awarn(note, op_id=f"nonverdict:{marker}:{inputs.retry_count}")
    try:
        if messages:
            save_review_artifacts(inputs.review_save_dir, messages, None, inputs.manifest,
                                  tool_call_count=tool_calls, retry_count=inputs.retry_count,
                                  job_id=inputs.job_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Reviewer: could not persist non-verdict trail - job_id=%s: %s",
                       inputs.job_id, exc)
    # A review that failed BEFORE the loop (unvalidated execution, malformed plan, missing
    # deterministic evidence, no renderable geometry, absent targets) still leaves the same
    # canonical record - that is the whole point of emitting on every exit.
    record = _pre_loop_record(inputs, marker)
    return _reviewer_return(api_failure=marker,
                            agent_run_records=[record] if record else [])


def _pre_loop_record(inputs: VisualReviewInteractionInputs | _EarlyRefusalInputs,
                     marker: str) -> dict | None:
    try:
        from meshpipeline.agents.loop.accounting import AgentRunAccountant
        from meshpipeline.agents.loop.diagnostics import sanitized
        from meshpipeline.agents.reviewer.diagnostics import ReviewRunExtension
        from meshpipeline.contracts.agent_loop import AgentRole, LoopExit, LoopLimits
        acct = AgentRunAccountant(role=AgentRole.reviewer, job_id=inputs.job_id,
                                  limits=LoopLimits(), pipeline_attempt=inputs.retry_count,
                                  agent_attempt=inputs.retry_count)
        return sanitized(acct.report(exit=LoopExit.policy_abort,
                                     extension=ReviewRunExtension(), failure_marker=marker))
    except Exception as exc:  # noqa: BLE001 - diagnostics never fail a review
        logger.warning("Reviewer: pre-loop record not built - job_id=%s: %s", inputs.job_id, exc)
        return None


async def _render_failure_result(inputs: VisualReviewInteractionInputs,
                           exc: ReviewRenderError) -> dict:
    if exc.category is ReviewEvidenceFailure.RENDERER_UNAVAILABLE:
        marker = "reviewer_render_unavailable"
    else:
        marker = "reviewer_evidence_missing"
    logger.error("Reviewer: visual verification unavailable - job_id=%s (%s): %s",
                 inputs.job_id, marker, exc.detail)
    return await _nonverdict(inputs, marker, _nonverdict_note(marker),
                       f"[{exc.category.value.upper()}] {exc.detail}")


async def _publish_inspection_image(publish: ExecutionEventPublisher | None,
                                    image_b64: str, op_id: str = "") -> None:
    if publish is None or not image_b64:
        return
    try:
        from meshpipeline.trace.policy import RAW, current_mode
        if current_mode() != RAW:
            # the activity is still reported - by the tool trace, in words
            return
        await publish.ascreenshot(image_b64, op_id=op_id)
    except StaleExecutionPublish:
        raise
    except Exception:      # observability never fails a review
        pass
