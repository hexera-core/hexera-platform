# Responsibility: Run one review invocation and turn what was observed into findings and a verdict.
# Owns: the review loop binding, evidence recording per operation, and findings parsing.
# Boundaries: it interprets observations; it renders nothing and re-runs no gate.
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from meshpipeline.agents.loop.runner import run_agent_loop
from meshpipeline.agents.loop.tracing import ExecutionTraceContext
from meshpipeline.agents.reviewer.eligibility import (
    AxisFinding,
    deterministic_evidence_complete,
)
from meshpipeline.agents.reviewer.loop_policy import (
    MARKER_EVIDENCE_MISSING,
    MARKER_EXHAUSTED,
    REVIEWER_NO_PROGRESS_THRESHOLD,
    ReviewLoopPolicy,
)
from meshpipeline.agents.reviewer.render_runtime import ReviewerRenderRuntime
from meshpipeline.agents.reviewer.tools import REVIEWER_TOOLS
from meshpipeline.contracts import rationale as _rationale
from meshpipeline.contracts.agent_loop import AgentRole, LoopExit, LoopLimits
from meshpipeline.contracts.event_stream import ExecutionEventPublisher
from meshpipeline.contracts.evidence_ledger import (
    EvidenceLedger,
    InspectionTargetRef,
)
from meshpipeline.contracts.model_inference import ModelRoundResult
from meshpipeline.contracts.review_outcome import ReviewVerdict

logger = logging.getLogger(__name__)

_OPENING_VIEW = "iso"
_OPENING_ARTIFACT = "mesh_paths.surface"

# The provider callable: (messages, tools, job_id, user_id) -> ONE normalized round result.
ProviderCall = Callable[..., Awaitable[ModelRoundResult]]


def _default_total_timeout() -> float:
    from meshpipeline.agents.reviewer import settings as _rcfg
    return float(_rcfg.REVIEWER_TOTAL_TIMEOUT_SECONDS)


# the unified verdict schema (evidence-linked findings)
_VIEWER_TOOL_NAMES = frozenset({
    "set_camera_preset", "move_camera", "rotate_camera", "zoom", "go_to_coordinates",
    "zoom_to_region", "inspect_region", "toggle_patch", "reset_view", "set_navigation_defaults",
})
def _tool_name(tool: Any) -> str:
    return tool["function"]["name"]


_VIEWER_TOOLS = [t for t in REVIEWER_TOOLS if _tool_name(t) in _VIEWER_TOOL_NAMES]

_SUBMIT_FINDINGS_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_findings",
        "description": (
            "Submit your complete review. Every review axis you were given must appear exactly "
            "once, with a nonblank finding and the evidence ids (shown to you after each "
            "successful inspection, e.g. 't-003') that substantiate it. You do NOT declare a "
            "verdict: PASS or FAIL is derived from these findings once they are accepted. A "
            "submission is accepted only when every obligation is met and every finding is "
            "grounded in evidence you actually produced. Prose without an evidence id is never "
            "accepted, and resubmitting unchanged findings is rejected the same way."),
        "parameters": {
            "type": "object",
            "properties": {
                "axis_findings": {
                    "type": "array",
                    "description": "One entry per composed review axis.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "axis_key": {"type": "string",
                                         "description": "the exact composed axis name"},
                            "finding": {"type": "string",
                                        "description": "what you concluded for this axis"},
                            "evidence_ids": {
                                "type": "array", "items": {"type": "string"},
                                "description": "ledger evidence ids that ground this finding"},
                            "passed": {
                                "type": "boolean",
                                "description": ("true if the mesh PASSES this axis, false if it "
                                                "fails it. A false must be grounded by a failed "
                                                "gate/metric or a target inspection you produced "
                                                "- an unsupported false is rejected.")},
                        },
                        "required": ["axis_key", "finding", "evidence_ids", "passed"],
                        "additionalProperties": False,
                    },
                },
                "rebuild_required": {
                    "type": "boolean",
                    "description": ("true ONLY if the mesh is the wrong approach and no adjustment "
                                    "recovers it; always false on PASS."),
                },
                "reasoning": {
                    "type": "string",
                    "description": "the in-depth report handed to the builder verbatim.",
                },
            },
            "required": ["axis_findings", "rebuild_required", "reasoning"],
            "additionalProperties": False,
        },
    },
}

# The COMPLETE roster the reviewer receives: navigation + ONE submission. There is no
# separate verdict declaration - the application derives PASS/FAIL from accepted findings.
UNIFIED_TOOLS = [*_VIEWER_TOOLS, _SUBMIT_FINDINGS_TOOL]


def _flag_findings_property(phase: str, flags: tuple) -> dict:
    from meshpipeline.contracts.human_flags import (
        PARENT_STATUSES,
        PHASE_REBUILT,
        REBUILT_STATUSES,
    )
    rebuilt = phase == PHASE_REBUILT
    statuses = list(REBUILT_STATUSES if rebuilt else PARENT_STATUSES)
    return {
        "type": "array",
        "description": (
            "One entry per region the engineer flagged, identified by its ordinal. "
            + ("This is the REBUILD they asked for: for each flag, compare the rebuilt mesh "
               "against the baseline recorded on the mesh they disputed and against what the "
               "builder says it changed. `resolved` and `not_reproduced` both assert something "
               "about THIS mesh and require the measurements you took here."
               if rebuilt else
               "Establish the baseline: for each flag, say whether the reported problem is "
               "actually present on the mesh the engineer disputed.")
            + f" Exactly {len(flags)} entr{'y' if len(flags) == 1 else 'ies'} are required."),
        "items": {
            "type": "object",
            "properties": {
                "ordinal": {"type": "integer",
                            "description": "the flag's number, as given to you"},
                "status": {"type": "string", "enum": statuses},
                "observation": {"type": "string",
                                "description": "what you actually saw at this region"},
                "explanation": {"type": "string", "description": "why that is your conclusion"},
                "measurements": {"type": "string",
                                 "description": "the values you measured here"},
            },
            "required": ["ordinal", "status", "observation", "explanation", "measurements"],
            "additionalProperties": False,
        },
    }


def submission_tools(user_dispute=None, phase: str = "") -> list:
    # The roster is IDENTICAL to the historical one unless a human raised flags, so no ordinary
    # review sees a changed tool surface.
    from meshpipeline.contracts.human_flags import flags_of
    flags = tuple(flags_of(user_dispute))
    if not flags:
        return UNIFIED_TOOLS
    import copy

    tool: dict[str, Any] = copy.deepcopy(_SUBMIT_FINDINGS_TOOL)
    fn: dict[str, Any] = tool["function"]
    params: dict[str, Any] = fn["parameters"]
    params["properties"]["flag_findings"] = _flag_findings_property(phase, flags)
    params["required"] = [*params["required"], "flag_findings"]
    fn["description"] += (
        " This review also carries the engineer's own flagged regions: every one of them needs "
        "its own entry in `flag_findings`, and a submission that omits, duplicates or invents a "
        "flag is rejected.")
    return [*_VIEWER_TOOLS, tool]


@dataclass(frozen=True)
class UnifiedReviewOutcome:

    verdict: str = ""                        # "PASS"/"FAIL"/"" (non-verdict)
    reasoning: str = ""
    rebuild_required: bool = False
    findings: tuple[AxisFinding, ...] = ()
    flag_findings: tuple = ()          # per human flag; empty on a normal run
    tool_calls: int = 0
    api_failure: str = ""                    # non-empty => non-verdict; the marker
    failure_class: str = ""                  # truthful classification for a non-verdict
    # the audit trail, so node_reviewer can persist + train without re-owning the loop
    messages: tuple = ()
    tool_records: tuple = ()
    llm_rounds: int = 0
    # The sanitized canonical run record for THIS invocation (v8 append-only history).
    run_record: dict = field(default_factory=dict)


# evidence recording (neutral: types the session's own result)
def record_operation_evidence(ledger: EvidenceLedger, ev, inventory: dict, operation: str) -> str:
    if ev is None:
        return ""
    if not getattr(ev, "image_ref", ""):
        detail = "; ".join(getattr(ev, "diagnostics", ()) or ()) or "no usable image produced"
        return ledger.add_validation(f"{operation}: {detail}", operation)
    covers = getattr(ev, "covers_target", "")
    if covers and covers in inventory:
        target = inventory[covers]
        return ledger.add_target_inspection(
            InspectionTargetRef(target.kind, covers), operation, image_ok=True, covered=True)
    if getattr(ev, "view_id", ""):
        return ledger.add_render_view(ev.view_id, getattr(ev, "artifact_key", "") or "",
                                      operation, image_ok=True)
    return ledger.add_render_view("", getattr(ev, "artifact_key", "") or "", operation,
                                  image_ok=True)


# The COMPLETE set of properties one axis finding may carry. Anything else - including the
# retired `satisfied` - is an unknown field and rejects the submission.
FINDING_FIELDS = frozenset({"axis_key", "finding", "evidence_ids", "passed"})


def parse_findings(args: dict) -> tuple[tuple[AxisFinding, ...], tuple[str, ...]]:
    problems: list[str] = []
    raw = args.get("axis_findings")
    if not isinstance(raw, list):
        return (), (f"axis_findings must be a list, got {type(raw).__name__}",)

    out: list[AxisFinding] = []
    for i, f in enumerate(raw):
        where = f"axis_findings[{i}]"
        if not isinstance(f, dict):
            problems.append(f"{where} must be an object, got {type(f).__name__}")
            continue

        unknown = sorted(set(f) - FINDING_FIELDS)
        if unknown:
            problems.append(f"{where} has unknown field(s) {', '.join(unknown)} - the only "
                            f"accepted fields are {', '.join(sorted(FINDING_FIELDS))}")
        missing = sorted(FINDING_FIELDS - set(f))
        if missing:
            problems.append(f"{where} is missing required field(s) {', '.join(missing)}")
        if unknown or missing:
            continue

        # `passed` must be a LITERAL JSON boolean. `type(...) is bool` deliberately, because
        # isinstance(1, bool) is False but isinstance(True, int) is True - a plain `isinstance`
        # check on int would let 1/0 through.
        judgement = f["passed"]
        if type(judgement) is not bool:
            problems.append(f"{where}.passed must be the JSON boolean true or false, got "
                            f"{judgement!r}")
        key = f["axis_key"]
        if not isinstance(key, str) or not key.strip():
            problems.append(f"{where}.axis_key must be a non-empty string, got {key!r}")
        text = f["finding"]
        if not isinstance(text, str):
            problems.append(f"{where}.finding must be a string, got {type(text).__name__}")
        eids = f["evidence_ids"]
        if not isinstance(eids, list) or not all(isinstance(e, str) for e in eids):
            problems.append(f"{where}.evidence_ids must be a list of strings")
        if problems:
            continue

        out.append(AxisFinding(axis_key=key.strip(), finding=text,
                               evidence_ids=tuple(eids), passed=judgement))

    return tuple(out), tuple(problems)


def _evidence_catalog(ledger: EvidenceLedger) -> str:
    lines: list[str] = []
    for g in ledger.gates():
        if g.usable:
            lines.append(f"  {g.evidence_id}: gate '{g.gate_key}' {g.status.value}")
    for m in ledger.metrics():
        if m.usable:
            ok = "acceptable" if m.status.value == "pass" else "NOT acceptable"
            lines.append(f"  {m.evidence_id}: metric '{m.metric_key}' = {m.value!r} ({ok})")
    for v in ledger.render_views():
        if v.usable and v.view_id:
            lines.append(f"  {v.evidence_id}: opening '{v.view_id}' view of the mesh")
    if not lines:
        return ""
    return ("Evidence already on file - cite these ids in the axes that call for a metric, gate or "
            "the opening view:\n" + "\n".join(lines))


def _append_evidence_id(content: Any, evidence_id: str) -> Any:
    if not evidence_id:
        return content
    note = f"[evidence: {evidence_id}]"
    if isinstance(content, list):
        return [*content, {"type": "text", "text": note}]
    if isinstance(content, str):
        return f"{content}\n{note}"
    return content


async def run_unified_review(
    *,
    plan,
    ledger: EvidenceLedger,
    runtime: ReviewerRenderRuntime,
    opening,
    system_prompt: str,
    opening_context_text: str,
    provider_call: ProviderCall,
    max_rounds: int,
    job_id: str = "",
    user_id: str = "",
    publish: ExecutionEventPublisher | None = None,
    obligations: tuple | None = None,
    total_timeout_s: float | None = None,
    pipeline_deadline_epoch: float | None = None,
    attempt: int = 0,
    user_dispute=None,
    dispute_phase: str = "",
) -> UnifiedReviewOutcome:
    # Part 6: refuse to call the provider when required deterministic evidence is missing. That is
    # an assurance-evidence failure, never provider downtime.
    complete, missing = deterministic_evidence_complete(plan, ledger)
    if not complete:
        logger.error("unified review: deterministic evidence incomplete %s - job_id=%s",
                     missing, job_id)
        outcome = UnifiedReviewOutcome(
            api_failure=MARKER_EVIDENCE_MISSING, failure_class="deterministic_evidence_missing")
        _emit_pre_loop_record(plan, ledger, job_id, attempt, LoopExit.policy_abort,
                              MARKER_EVIDENCE_MISSING)
        return outcome

    # Opening view evidence - only when geometry is truthfully present and the image validated.
    if opening.has_geometry and opening.initial_screenshot_b64:
        ledger.add_render_view(_OPENING_VIEW, _OPENING_ARTIFACT, "open", image_ok=True)
    inventory = {t.target_id: t for t in opening.inspection_targets}
    catalog = _evidence_catalog(ledger)
    if catalog:
        opening_context_text = f"{opening_context_text}\n\n{catalog}"

    user_content: Any = [
        {"type": "image_url",
         "image_url": {"url": f"data:image/png;base64,{opening.initial_screenshot_b64}"}},
        {"type": "text", "text": opening_context_text},
    ] if opening.initial_screenshot_b64 else opening_context_text
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    # ONE monotonic aggregate deadline for this whole review invocation, settled here and
    # then ENFORCED by the canonical loop.: capped at the top-level pipeline's remaining time.
    budget = float(total_timeout_s) if total_timeout_s is not None else _default_total_timeout()
    if pipeline_deadline_epoch:
        from meshpipeline.application.pipeline_budget import cap_child_budget
        budget = cap_child_budget(budget, pipeline_deadline_epoch)

    policy = ReviewLoopPolicy(
        plan=plan, ledger=ledger, runtime=runtime, obligations=obligations, inventory=inventory,
        publish=publish, user_dispute=user_dispute, dispute_phase=dispute_phase,
        limits_=LoopLimits(max_rounds=max_rounds, total_timeout_s=budget,
                           # ACTIVATED: the Builder's production stall threshold, the only new
                           # enforcement in this cutover. Tool-call, category and warning limits
                           # stay unset - recorded, not enforced.
                           no_progress_threshold=REVIEWER_NO_PROGRESS_THRESHOLD))

    result = await run_agent_loop(
        driver=policy, provider_call=provider_call, messages=messages,
        tools=submission_tools(user_dispute, dispute_phase),
        job_id=job_id, user_id=user_id, pipeline_attempt=attempt, agent_attempt=attempt,
        deadline_s=budget, append_tool_result=_append_tool_result,
        # Structured tool_call/tool_result now carry every tool the reviewer runs, with
        # its own public label. The old per-round `action` list said the same thing in
        # weaker words and would double every row on the timeline.
        execution_trace=(ExecutionTraceContext(publisher=publish, job_id=str(job_id),
                                              role="reviewer", attempt=int(attempt))
                         if publish is not None else None),
        provider_failure_marker=_marker_for_exit)

    return await _outcome_from(result, policy, messages, publish=publish,
                               job_id=job_id)


def _marker_for_exit(exit_reason: LoopExit) -> str:
    if exit_reason is LoopExit.deadline_exhausted:
        return MARKER_EXHAUSTED
    if exit_reason is LoopExit.no_progress:
        return MARKER_EVIDENCE_MISSING
    if exit_reason is LoopExit.rounds_exhausted:
        return MARKER_EXHAUSTED
    return MARKER_EVIDENCE_MISSING


_FAILURE_CLASS = {
    LoopExit.deadline_exhausted: "reviewer_deadline_exhausted",
    LoopExit.no_progress: "eligibility_non_convergence",
    LoopExit.rounds_exhausted: "reviewer_exhausted",
    LoopExit.provider_failed: "provider_unavailable",
    LoopExit.policy_abort: "deterministic_evidence_missing",
}


async def _outcome_from(result, policy: ReviewLoopPolicy, messages: list[dict], *,
                        publish: ExecutionEventPublisher | None,
                        job_id: str) -> UnifiedReviewOutcome:
    tally = result.record.tally
    from meshpipeline.agents.loop.diagnostics import sanitized
    common = {"messages": tuple(messages), "llm_rounds": tally.rounds,
              "tool_calls": tally.tool_calls, "run_record": sanitized(result.record)}
    if result.exit is LoopExit.terminal_action and policy.accepted is not None:
        # Eligibility accepted the submission and derived the verdict from the SAME findings the
        # per-axis record is built from, so the two can never disagree.
        verdict = policy.accepted.verdict
        assert verdict is not None, "an accepted decision always carries a verdict"
        logger.info("Reviewer: eligibility accepted - verdict=%s rounds=%d - job_id=%s",
                    verdict.value, tally.rounds, job_id)
        # The application's own account of the verdict, from the accepted findings -
        # not from anything the model said about them.
        if publish is not None:
            await _rationale.areviewer_verdict(
                publish, passed=verdict is ReviewVerdict.passed,
            # AxisFinding is the canonical typed record; a failed axis is one whose
            # own `passed` is false. The rationale names them rather than saying
            # "it failed", because a list of criteria is what the user can act on.
                failed_axes=tuple(
                    f.axis_key for f in (policy.accepted_findings or ())
                    if getattr(f, "passed", True) is False))
        return UnifiedReviewOutcome(
            verdict=("PASS" if verdict is ReviewVerdict.passed else "FAIL"),
            reasoning=policy.accepted_reasoning,
            rebuild_required=policy.accepted_rebuild_required and verdict is ReviewVerdict.failed,
            findings=policy.accepted_findings,
            flag_findings=policy.accepted_flag_findings, **common)
    marker = result.failure_marker or MARKER_EVIDENCE_MISSING
    if publish is not None:
        await _rationale.areviewer_evidence_incomplete(publish)
    logger.error("Reviewer: no eligible verdict - exit=%s marker=%s rounds=%d "
                 "submissions=%d rejections=%d - job_id=%s",
                 result.exit.value, marker, tally.rounds, policy.submissions,
                 policy.rejections, job_id)
    return UnifiedReviewOutcome(
        api_failure=marker,
        failure_class=_FAILURE_CLASS.get(result.exit, "reviewer_exhausted"), **common)


def _emit_pre_loop_record(plan, ledger, job_id: str, attempt: int, exit_reason: LoopExit,
                          marker: str) -> None:
    try:
        from meshpipeline.agents.loop.accounting import AgentRunAccountant
        from meshpipeline.agents.loop.diagnostics import emit
        policy = ReviewLoopPolicy(plan=plan, ledger=ledger, runtime=None,
                                  limits_=LoopLimits())
        acct = AgentRunAccountant(role=AgentRole.reviewer, job_id=job_id, limits=LoopLimits(),
                                  pipeline_attempt=attempt, agent_attempt=attempt)
        emit(acct.report(exit=exit_reason, extension=policy.extension(), failure_marker=marker))
    except Exception as exc:  # noqa: BLE001 - diagnostics never fail a review
        logger.warning("Reviewer: pre-loop run record not emitted - job_id=%s: %s", job_id, exc)


def _append_tool_result(messages: list[dict], tool_call_id: str, content: Any) -> None:
    if isinstance(content, list):
        text = " ".join(i["text"] for i in content
                        if isinstance(i, dict) and i.get("type") == "text")
        messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": text})
        for i in content:
            if isinstance(i, dict) and i.get("type") == "image_url":
                messages.append({"role": "user", "content": [i]})
    else:
        messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": content})


__all__ = [
    "UNIFIED_TOOLS",
    "submission_tools",
    "UnifiedReviewOutcome",
    "run_unified_review",
    "record_operation_evidence",
]
