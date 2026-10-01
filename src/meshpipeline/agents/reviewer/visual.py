# Responsibility: Run the reviewer node: inspect the mesh and return a verdict with its evidence.
# Boundaries: it judges an ALREADY-VALIDATED mesh - the executor's gates ran first.
# Collaborates with: agents/reviewer/unified.py, render_runtime.py and sandbox/.
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import meshpipeline.agents.reviewer.settings as rcfg
from meshpipeline.agents.reviewer.context import build_review_prompt
from meshpipeline.agents.reviewer.deterministic_evidence import collect_deterministic_evidence
from meshpipeline.agents.reviewer.eligibility import (
    deterministic_evidence_complete,
    missing_target_obligations,
    validate_plan,
)
from meshpipeline.agents.reviewer.interaction_inputs import VisualReviewInteractionInputs
from meshpipeline.agents.reviewer.loop_policy import MARKER_STALLED
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


def _with_adjudicated_deviations(brief: str, caveats: list) -> str:
    """A caveated delivery candidate reaches review with its requirement near-misses ALREADY
    adjudicated by deterministic code - they will be stated on delivery. Without saying so,
    the brief's own conformance language invites the reviewer to FAIL the mesh for the exact
    deviation being disclosed, and the ladder is already spent. The reviewer judges QUALITY;
    it can neither waive nor add to this list."""
    if not caveats:
        return brief
    rows = "; ".join(
        f"{c.get('direction')}: requested {c.get('requested'):g}, "
        f"measured {c.get('measured'):g} reference-lengths"
        for c in caveats)
    return (brief
            + "\n\nADJUDICATED REQUIREMENT DEVIATIONS (application-owned): " + rows
            + ". These measured near-misses are accepted for delivery and will be stated to "
              "the user verbatim - judge the mesh's QUALITY on every axis as normal, but do "
              "not fail the mesh for these deviations themselves.")


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
    # A RERUN reviews the same mesh again after a review that ended without a verdict
    # (pipeline/graph.node_review_retry). Its trail, its event ids and its words are its own.
    rerun = int(state.get("review_rerun_count", 0) or 0)
    _scope = f"{retry_count}" + (f":rerun{rerun}" if rerun else "")
    review_save_dir = workspace / (f"review_{retry_count + 1}"
                                   + (f"_rerun{rerun}" if rerun else ""))
    logger.info("Reviewer: starting - job_id=%s engine=%s attempt=%d rerun=%d",
                job_id, engine, retry_count, rerun)

    def _read_txt(filename: str) -> str:
        p = workspace / filename
        try:
            return p.read_text(encoding="utf-8").strip() if p.exists() else ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("Reviewer: could not read %s - job_id=%s: %s", filename, job_id, exc)
            return ""

    # WHAT THE FINDINGS ARE JUDGED AGAINST (agents/reviewer/review_policy): the user's request, the
    # acceptance criteria and the setup they confirmed - the only text a finding may quote to ask
    # for a rebuild or to call the mesh the wrong problem.
    from meshpipeline.agents.reviewer.review_policy import builder_levers, measurements_from
    _request_txt = _read_txt("request.txt") or state.get("request_txt", "")
    _brief_txt = _read_txt("review_brief.txt") or state.get("review_brief_txt", "")
    _confirmed = confirmed_setup(state)
    _history = tuple(h for h in (state.get("review_history") or []) if isinstance(h, dict))
    _evidence = (_evidence_key(engine=engine, purpose=purpose, request=_request_txt,
                               brief=_brief_txt, confirmed=_confirmed, manifest=manifest,
                               workspace=workspace, spec=spec)
                 if state.get("executor_success") is True else "")
    # THE SAME MESH, SHOWN THE SAME EVIDENCE, IS NEVER JUDGED TWICE. A second look could only re-roll
    # the verdict (job 4d8b318d failed 59.6% layer coverage on attempt 1 and passed the same 59.6% on
    # attempt 3). A rerun after a review that reached no verdict, and a dispute, are judged afresh.
    _reused = (None if rerun or state.get("user_dispute") or not _evidence else
               next((h for h in reversed(_history) if h.get("evidence_key") == _evidence
                     and h.get("verdict") in ("PASS", "FAIL")), None))

    # THE reviewer's execution publisher. The review runs inside the graph, under the
    # claim taken before it started, so every event it publishes is ownership-checked.
    _publish = execution_publisher(job_id, agent="reviewer")
    await _publish.astage(op_id=f"inspect:{_scope}")
    await _publish.anote("This mesh, and everything a review would be shown, is identical to one "
                         "already reviewed in this run - that review stands (nothing is judged "
                         "twice)" if _reused is not None else
                         "Inspecting the mesh against your brief" if not rerun else
                         "The last review stopped before it reached a verdict - reviewing the "
                         "same mesh again (nothing is rebuilt)",
                         op_id=f"inspect:{_scope}")
    if _reused is not None:
        return _reuse_review(_reused, retry_count=retry_count, workspace=workspace,
                             history=_history, evidence_key=_evidence,
                             requirement_caveats=state.get("requirement_caveats") or [])

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
            manifest=manifest, retry_count=retry_count, rerun=rerun,
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
        review_brief=_with_adjudicated_deviations(
            _brief_txt + (f"\n\n{_confirmed}" if _confirmed else ""),
            state.get("requirement_caveats") or []),
        request=_request_txt,
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
        rerun=rerun,
        brief_text="\n".join(t for t in (_request_txt, _brief_txt, _confirmed) if t),
        levers=builder_levers(spec, purpose),
        measured=tuple(measurements_from(manifest, state).items()),
        evidence_key=_evidence,
        review_history=_history,
        requirement_caveats=tuple(state.get("requirement_caveats") or ()),
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
            # The reviewer's own inspection renders - this opening one, then every view it takes
            # in the loop below. Safe mode publishes NOTHING for these - the reader is told an
            # image was produced (as tool activity), not handed the image - so the bytes never
            # enter the public backlog at all. Raw mode publishes them, sanitized, through the
            # same event.
            pictures = _LivePictures(_publish, _scope)
            await pictures.opening(opening.initial_screenshot_b64)

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
                # what the viewer can actually draw - the manifest also names boundaries (the far
                # field, a ground plane) that no review geometry reaches. The session's ENTITIES,
                # the same set toggle_patch and go_to_coordinates screen against: an engine whose
                # targets are groups or openings (gmsh, vmtk) has no PATCH targets at all, and an
                # empty set would read as "nothing here can be shown". None when unknown.
                renderable_patches=await _viewer_entities(runtime),
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
                attempt=inputs.retry_count, rerun=inputs.rerun,
                user_dispute=inputs.user_dispute, dispute_phase=inputs.dispute_phase,
                on_picture=pictures, brief_text=inputs.brief_text, levers=inputs.levers,
                measured=dict(inputs.measured))
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
    # This run's concluded reviews, one per reviewed mesh: what the review saw (its evidence key),
    # where the mesh is, and what it found. Append-only. It lets an identical mesh keep its review
    # instead of being judged twice, and lets the run deliver its best gate-passing mesh.
    "review_history",
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


async def _viewer_entities(runtime) -> list[str] | None:
    """The names the viewer can toggle and frame, or None when the runtime does not say."""
    caps = getattr(runtime, "capabilities", None)
    if caps is None:
        return None
    try:
        return list((await caps()).entities) or None
    except Exception:  # noqa: BLE001 - a prompt hint must never cost the review
        logger.warning("Reviewer: viewer entities unavailable for the prompt", exc_info=True)
        return None


def _findings_dump(plan, ledger, outcome: UnifiedReviewOutcome, attempt: int) -> list[dict]:
    _owner = {ax.name: getattr(ax, "owner", "") for ax in plan.axes}
    rows = []
    for f in outcome.findings:
        row = {
            "axis_key":     f.axis_key,
            "owner":        _owner.get(f.axis_key, ""),
            "passed":       f.passed,
            "finding":      f.finding,
            "evidence_ids": list(f.evidence_ids),
            "attempt":      attempt,
        }
        # WHAT THE APPLICATION DECIDED this finding may do (review_policy) - on every finding that
        # did not pass, so routing, delivery and the console read one judgement, never the model's.
        j = (outcome.judgements or {}).get(f.axis_key)
        if not f.passed and j is not None:
            row.update({
                "blocking": j.blocking,          # a WRONG_PROBLEM class, or "" - only these fail a job
                "improve": j.improve,            # may ask the builder for a rebuild
                "concern_reason": j.reason,      # why it is only a concern
                "severity": j.severity,          # how prominently a concern is shown
                "brief_requirement": f.brief_requirement,
                "builder_change": f.builder_change,
                "change_request": f.change_request,
            })
        rows.append(row)
    return rows


def confirmed_setup(state) -> str:
    """The setup the user CONFIRMED before meshing, in words a finding can quote: the boundaries
    and their roles, the flow direction, the reference length and the outer-domain margins. Read
    from the approved state, never from the builder's configuration."""
    lines: list[str] = []
    patches = [p for p in (state.get("intake_patches") or []) if isinstance(p, dict)]
    if patches:
        lines.append("Boundaries: " + "; ".join(
            f"{p.get('name')} as {p.get('type')}" for p in patches if p.get("name")) + ".")
    if state.get("flow_axis"):
        lines.append(f"Flow direction: {state.get('flow_axis')}.")
    ruler = state.get("reference_length_m")
    if isinstance(ruler, (int, float)) and not isinstance(ruler, bool) and ruler > 0:
        lines.append(f"Reference length: {float(ruler):.6g} m.")
    ext = state.get("requested_extents")
    if isinstance(ext, dict):
        parts = [f"{k} {float(v):g}" for k, v in ext.items()
                 if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if parts:
            lines.append("Outer-domain margins in reference lengths: " + ", ".join(parts) + ".")
    if state.get("dimensionality"):
        lines.append(f"Dimensionality: {state.get('dimensionality')}.")
    if not lines:
        return ""
    return ("CONFIRMED SETUP (approved by the user before meshing; quote it word for word when a "
            "finding contradicts it):\n  " + "\n  ".join(lines))


def _evidence_key(*, engine: str, purpose: str, request: str, brief: str, confirmed: str,
                  manifest: dict, workspace: Path, spec) -> str:
    """The identity of everything a review of this mesh is shown: the brief, the confirmed setup,
    the measurements the gates took and the configuration that built it. Equal keys mean an
    identical mesh with identical evidence."""
    authored: dict = {}
    pol = getattr(spec, "run_policy", None)
    for rel in (pol.required_files if pol else ()):
        p = Path(workspace) / rel
        try:
            if p.is_file():
                authored[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            authored[rel] = "unreadable"
    body = json.dumps({
        "engine": engine, "purpose": purpose, "request": request, "brief": brief,
        "confirmed": confirmed, "quality": (manifest or {}).get("quality"),
        "cells": (manifest or {}).get("cell_count"),
        "patches": sorted(((manifest or {}).get("patches") or {}).keys()),
        "authored": authored,
    }, sort_keys=True, default=str)
    return hashlib.sha256(body.encode()).hexdigest()[:32]


def _history_entry(*, attempt: int, workspace: Path, evidence_key: str, verdict: str,
                   findings: list[dict], feedback: str, requirement_caveats) -> dict:
    return {"attempt": int(attempt), "workspace": str(workspace), "evidence_key": evidence_key,
            "verdict": verdict, "findings": findings, "feedback": feedback[:4000],
            "requirement_caveats": list(requirement_caveats or [])}


def _reuse_review(entry: dict, *, retry_count: int, workspace: Path, history: tuple,
                  evidence_key: str, requirement_caveats) -> dict:
    """The earlier review of this identical mesh, as THIS attempt's verdict. Its requests for a
    rebuild are spent - the rebuild produced this same mesh - so none of them asks again; a finding
    that made the mesh the wrong problem still does."""
    findings = []
    for f in entry.get("findings") or []:
        row = dict(f, attempt=retry_count)
        if row.get("passed") is False:
            row["improve"] = False
            row.setdefault("concern_reason", "")
            if not row.get("blocking"):
                row["concern_reason"] = "a rebuild produced this same mesh again"
        findings.append(row)
    verdict = str(entry.get("verdict") or "")
    feedback = str(entry.get("feedback") or "")
    return _reviewer_return(
        reviewer_result=f"{verdict}\n(identical to the review of attempt {entry.get('attempt')})",
        reviewer_verdict=verdict,
        reviewer_feedback=feedback if verdict == "FAIL" else "",
        reviewer_axis_findings=findings,
        reviewer_rebuild_required=False,
        reviewer_tool_calls=[0],
        review_history=[*history, _history_entry(
            attempt=retry_count, workspace=workspace, evidence_key=evidence_key,
            verdict=verdict, findings=findings, feedback=feedback,
            requirement_caveats=requirement_caveats)],
    )


def _builder_feedback(reasoning: str, findings: list[dict]) -> str:
    """What the builder is handed after a review that did not pass: the changes the review may ask
    for, each with the requirement it serves, then the reviewer's report. Concerns are not asks."""
    asks = [f for f in findings if f.get("improve") is True]
    if not asks:
        return reasoning
    lines = ["The review asks for these changes:"]
    for f in asks:
        lines.append(f"- [{f.get('builder_change')}] {f.get('change_request')} "
                     f"(requirement: \"{f.get('brief_requirement')}\")")
    return "\n".join(lines) + ("\n\n" + reasoning if reasoning else "")


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
    feedback  = _builder_feedback(reasoning, findings) if verdict == "FAIL" else ""

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
        reviewer_feedback=feedback,
        reviewer_axis_findings=findings,
        reviewer_rebuild_required=outcome.rebuild_required,
        reviewer_tool_calls=[outcome.tool_calls],
        review_history=[*inputs.review_history, _history_entry(
            attempt=inputs.retry_count, workspace=inputs.workspace,
            evidence_key=inputs.evidence_key, verdict=verdict, findings=findings,
            feedback=feedback, requirement_caveats=inputs.requirement_caveats)],
        # The baseline is written by the phase that establishes it and never overwritten by the
        # phase that is judged against it - otherwise the comparison would be with itself.
        **({"dispute_flag_findings": HF.as_dicts(outcome.flag_findings)}
           if inputs.dispute_phase == HF.PHASE_PARENT and outcome.flag_findings else {}),
    )


def _nonverdict_note(marker: str) -> str:
    # What happened, and nothing about what to do next: a review that stopped this way may be
    # started again on the same mesh (pipeline/graph.node_review_retry), and the terminal message
    # owns the next step if it is not.
    if marker in ("reviewer_render_unavailable",):
        return ("Visual verification could not be completed because the mesh rendering step was "
                "unavailable. This is a problem on our side.")
    if marker == MARKER_STALLED:
        return ("The review stopped making progress before it reached a verdict. This is a "
                "problem on our side, not your mesh's.")
    if marker == "reviewer_exhausted":
        return ("The review ran out of time before it reached a verdict. This is a problem on "
                "our side, not your mesh's.")
    if _is_provider_failure(marker):
        return ("The review service is temporarily unavailable. This is a problem on our side.")
    return ("The review could not judge the mesh from the evidence it had, so it reached no "
            "verdict. This is a problem on our side.")


def _is_provider_failure(marker: str) -> bool:
    return marker not in {
        "reviewer_render_unavailable", "reviewer_evidence_missing", "reviewer_exhausted",
        MARKER_STALLED,
    }


@dataclass(frozen=True)
class _EarlyRefusalInputs:

    job_id: str
    publish: ExecutionEventPublisher
    review_save_dir: Path
    manifest: dict
    retry_count: int
    rerun: int = 0


async def _early_nonverdict(*, job_id: str, publish: ExecutionEventPublisher,
                            review_save_dir: Path, manifest: dict,
                      retry_count: int, marker: str, note: str, chain: str,
                      rerun: int = 0) -> dict:
    return await _nonverdict(
        _EarlyRefusalInputs(job_id=job_id, publish=publish, review_save_dir=review_save_dir,
                            manifest=manifest, retry_count=retry_count, rerun=rerun),
        marker, note, chain)


async def _nonverdict(inputs: VisualReviewInteractionInputs | _EarlyRefusalInputs, marker: str,
                      note: str, chain: str,
                *, tool_calls: int = 0, messages: list | None = None) -> dict:
    # the marker names WHICH non-verdict this is; the attempt separates genuine retries
    _rerun = int(getattr(inputs, "rerun", 0) or 0)
    await inputs.publish.awarn(note, op_id=f"nonverdict:{marker}:{inputs.retry_count}"
                                           + (f":rerun{_rerun}" if _rerun else ""))
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


# The most pictures one review puts on the live page AFTER its opening one. Each is 100-350 KB of
# base64 on the live stream, so the cap keeps a runaway review from pushing hundreds of them; a
# typical review takes about twenty views and repeats several. The model still sees every view.
LIVE_PICTURES_MAX = 24


class _LivePictures:
    """Puts the opening render, then each view the reviewer takes in its loop, on the live page.

    Every picture goes through `_publish_inspection_image` under its own id, so the same rules
    hold for all of them: only raw trace mode publishes, a failed publish costs the review nothing,
    and only a lost claim propagates - as it does for every other event this reviewer publishes.
    A view identical to one already sent (the reviewer often returns to the same camera) is not
    sent again and does not count toward the cap: the page would show the same picture twice."""

    def __init__(self, publish: ExecutionEventPublisher | None, scope: str,
                 limit: int = LIVE_PICTURES_MAX) -> None:
        self._publish = publish
        self._scope = scope
        self._limit = limit
        self._seen: set[bytes] = set()
        self.sent = 0

    async def opening(self, image_b64: str | None) -> None:
        await self._send(image_b64 or "", f"opening-render:{self._scope}")

    async def __call__(self, content: Any) -> None:
        await self._send(_image_b64(content), "")

    async def _send(self, image_b64: str, op_id: str) -> None:
        try:
            if not image_b64:
                return
            key = hashlib.sha256(image_b64.encode()).digest()
            if key in self._seen:
                return
            if not op_id:
                if self.sent >= self._limit:
                    return
                self.sent += 1
                op_id = f"review-render:{self._scope}:{self.sent}"
            self._seen.add(key)
            await _publish_inspection_image(self._publish, image_b64, op_id=op_id)
        except StaleExecutionPublish:
            raise
        except Exception:  # noqa: BLE001 - a picture for the page never costs the review
            logger.debug("Reviewer: live picture not published", exc_info=True)


def _image_b64(content: Any) -> str:
    """The base64 body of the image in a viewer tool's result, or '' when it carries none."""
    if not isinstance(content, list):
        return ""
    for part in content:
        if not (isinstance(part, dict) and part.get("type") == "image_url"):
            continue
        ref = part.get("image_url")
        url = ref.get("url") if isinstance(ref, dict) else None
        if isinstance(url, str) and url.startswith("data:image/"):
            _head, sep, body = url.partition(";base64,")
            if sep and body:
                return body
    return ""
