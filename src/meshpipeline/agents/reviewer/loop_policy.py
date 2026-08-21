# Responsibility: Decide what the review loop does next, from what remains uninspected.
# Owns: axis deficits, findings normalisation, and the stop conditions.
# Boundaries: policy over observed evidence; it inspects nothing itself.
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

from meshpipeline.agents.loop.accounting import ToolInvocation
from meshpipeline.agents.loop.driver import RoundDecision, ToolOutcome
from meshpipeline.agents.reviewer.diagnostics import ReviewRunExtension
from meshpipeline.agents.reviewer.eligibility import (
    AxisFinding,
    Eligibility,
    EligibilityDecision,
    evaluate_eligibility,
)
from meshpipeline.agents.reviewer.evidence_delta import EvidenceDelta, EvidenceSnapshot, diff
from meshpipeline.contracts.agent_loop import (
    AgentRole,
    LoopLimits,
    LoopStage,
    LoopTally,
    ProgressObservation,
)
from meshpipeline.contracts.evidence_ledger import EvidenceLedger

logger = logging.getLogger(__name__)

SUBMIT_FINDINGS = "submit_findings"

# The ONLY new enforcement in this cutover, and the only value it activates.
# 3 is not a guess, but be precise about the precedent: the Builder's now-retired
# StuckLoopDetector issued
# its escalated "change your strategy" correction on the 3rd identical call
# (BUILDER_STUCK_WARNING_AT) and aborted on the 4th - behaviour BuilderLoopPolicy now
# carries forward unchanged. The Reviewer terminates at 3 rather than
# correcting there, because its correction is already delivered on EVERY rejection - by the
# third unchanged one the model has had three specific, budget-aware corrections naming the
# same unresolved axes.
# It directly bounds the observed failure: DPW4 attempt 2 resubmitted against unchanged deficits
# seven consecutive times. Deterministic replay (test_dpw4_attempt2_replay.py) shows it ending
# that run at round 18 of 30 instead of burning the whole budget - validated before any live run.
REVIEWER_NO_PROGRESS_THRESHOLD = 3

# The failure markers a review execution may end with. All map to REVIEW_EVIDENCE_MISSING in
# errors.py - "we could not judge the mesh", never a statement about the mesh.
MARKER_EVIDENCE_MISSING = "reviewer_evidence_missing"
MARKER_EXHAUSTED = "reviewer_exhausted"


@dataclass(frozen=True)
class AxisDeficits:

    missing: tuple[str, ...] = ()
    duplicate: tuple[str, ...] = ()
    ungrounded: tuple[str, ...] = ()
    covered: tuple[str, ...] = ()
    invalid_evidence: tuple[str, ...] = ()

    def signature(self) -> str:
        body = "|".join(",".join(sorted(part)) for part in
                        (self.missing, self.duplicate, self.ungrounded, self.invalid_evidence))
        return hashlib.sha256(body.encode()).hexdigest()[:16]

    @property
    def clean(self) -> bool:
        return not (self.missing or self.duplicate or self.ungrounded or self.invalid_evidence)


def compute_deficits(required_axes: tuple[str, ...], findings: tuple[AxisFinding, ...],
                     ledger: EvidenceLedger) -> AxisDeficits:
    seen: dict[str, int] = {}
    for f in findings:
        seen[f.axis_key] = seen.get(f.axis_key, 0) + 1
    known = set(required_axes)
    cited_ok: set[str] = set()
    cited_bad: set[str] = set()
    for f in findings:
        for eid in f.evidence_ids:
            (cited_ok if eid in ledger else cited_bad).add(eid)
    return AxisDeficits(
        missing=tuple(sorted(a for a in required_axes if a not in seen)),
        duplicate=tuple(sorted(k for k, n in seen.items() if n > 1)),
        ungrounded=tuple(sorted(k for k in seen if k in known and not _has_usable_citation(
            k, findings, ledger))),
        covered=tuple(sorted(k for k in seen if k in known)),
        invalid_evidence=tuple(sorted(cited_bad)))


def _has_usable_citation(axis: str, findings: tuple[AxisFinding, ...],
                         ledger: EvidenceLedger) -> bool:
    for f in findings:
        if f.axis_key != axis:
            continue
        for eid in f.evidence_ids:
            record = ledger.get(eid)
            if record is not None and getattr(record, "usable", False):
                return True
    return False


def _signature(parts) -> str:
    return hashlib.sha256("|".join(sorted(parts)).encode()).hexdigest()[:16]


def normalize_findings(findings: tuple[AxisFinding, ...]) -> str:
    body = "|".join(f"{f.axis_key}:{','.join(sorted(f.evidence_ids))}"
                    for f in sorted(findings, key=lambda f: f.axis_key))
    return hashlib.sha256(body.encode()).hexdigest()[:16]


@dataclass
class ReviewLoopPolicy:

    plan: Any
    ledger: EvidenceLedger
    runtime: Any
    limits_: LoopLimits
    obligations: tuple | None = None
    inventory: dict = field(default_factory=dict)
    publish: Any = None
    role: AgentRole = AgentRole.reviewer
    # The human's flags and which artifact this review is judging. Absent on every
    # ordinary run, and then nothing below changes behaviour.
    user_dispute: Any = None
    dispute_phase: str = ""

    # attempt-local accumulators
    submissions: int = 0
    malformed_submissions: int = 0
    rejections: int = 0
    rejection_reasons: list[str] = field(default_factory=list)
    accepted: EligibilityDecision | None = None
    accepted_findings: tuple[AxisFinding, ...] = ()
    accepted_reasoning: str = ""
    accepted_rebuild_required: bool = False
    accepted_flag_findings: tuple = ()
    last_deficits: AxisDeficits | None = None
    last_findings_sig: str = ""
    last_submission_changed: bool = False
    delta: EvidenceDelta = field(default_factory=EvidenceDelta)
    _snapshot: EvidenceSnapshot | None = None       # evidence as of the last SUBMISSION
    _observed_at: EvidenceSnapshot | None = None    # evidence as of the last ROUND observation
    _round_progressed: bool = False
    _progress_signature: str = ""

    def __post_init__(self) -> None:
        self.required_axes = tuple(getattr(ax, "name", "") for ax in getattr(self.plan, "axes", ()))
        self._snapshot = EvidenceSnapshot.of(self.ledger)
        self._observed_at = self._snapshot

    # LoopDriver: the runner
    def limits(self) -> LoopLimits:
        return self.limits_

    def category_of(self, tool: str) -> str:
        from meshpipeline.agents.reviewer.render_runtime import (
            VIEWER_CONFIGURE_TOOLS,
            VIEWER_RENDERING_TOOLS,
        )
        if tool in VIEWER_RENDERING_TOOLS | VIEWER_CONFIGURE_TOOLS:
            return "navigation"
        if tool == SUBMIT_FINDINGS:
            return "submission"
        return "unknown"

    async def execute(self, invocation: ToolInvocation) -> ToolOutcome:
        from meshpipeline.agents.reviewer.unified import (
            _append_evidence_id,
            record_operation_evidence,
        )
        if invocation.parsed is None:
            return ToolOutcome(content=self._malformed_correction(), accepted=False)
        args = invocation.parsed

        if invocation.tool == SUBMIT_FINDINGS:
            return self._submit(args)

        typed = await self.runtime.execute_typed(invocation.tool, args)
        if typed.is_viewer_tool:
            eid = record_operation_evidence(self.ledger, typed.evidence, self.inventory,
                                            invocation.tool)
            return ToolOutcome(content=_append_evidence_id(typed.content, eid))
        return ToolOutcome(content=f"Unknown tool: {invocation.tool}", accepted=False)

    # the submission path
    def _submit(self, args: dict) -> ToolOutcome:
        from meshpipeline.agents.reviewer.unified import parse_findings
        from meshpipeline.contracts import human_flags as HF
        findings, problems = parse_findings(args)
        # THE PER-FLAG CONTRACT, parsed on the same path as the axes so a malformed flag result is
        # corrected the same way a malformed axis finding is - by asking again, never by inferring.
        flag_findings: tuple = ()
        if HF.expected_ordinals(self.user_dispute):
            flag_findings, flag_problems = HF.parse_flag_findings(
                args.get("flag_findings"), user_dispute=self.user_dispute,
                phase=self.dispute_phase or HF.PHASE_PARENT)
            problems = (*problems, *flag_problems)
        if problems:
            # MALFORMED. Nothing was constructed with an inferred judgement, eligibility is not
            # consulted, and no verdict can exist. It is not counted as a submission because
            # eligibility never evaluated one - it is counted, and stalls, on its own terms.
            self.malformed_submissions += 1
            self._mark_no_progress(f"malformed:{_signature(problems)}")
            return ToolOutcome(content=self._malformed_findings_correction(problems),
                               accepted=False)
        self.submissions += 1

        before = self._snapshot or EvidenceSnapshot.of(self.ledger)
        after = EvidenceSnapshot.of(self.ledger)
        cited = frozenset(e for f in findings for e in f.evidence_ids)
        self.delta = diff(before, after, cited=cited)
        self._snapshot = after

        sig = normalize_findings(findings)
        self.last_submission_changed = bool(self.last_findings_sig) and sig != self.last_findings_sig
        self.last_findings_sig = sig

        deficits = compute_deficits(self.required_axes, findings, self.ledger)
        # The typed findings go STRAIGHT to eligibility. Nothing assembles an aggregate verdict
        # string first; the verdict comes back on the decision, or there is none.
        decision = evaluate_eligibility(self.plan, self.ledger, findings,
                                        obligations=self.obligations)
        previous = self.last_deficits
        self.last_deficits = deficits

        # THE GATE. A human asked for specific regions to be fixed; a PASS that leaves one of them
        # unresolved or unassessed is not a pass, whatever the axes say. FAIL is unaffected - it is
        # already the answer the user needs - and this is a no-op on every non-dispute review.
        _blocking = HF.blocking_flag_reasons(
            flag_findings, phase=self.dispute_phase or HF.PHASE_PARENT,
            user_dispute=self.user_dispute)
        if _blocking and decision.outcome is Eligibility.PASS:
            self.rejections += 1
            _why = ("the engineer's flagged regions are not all satisfied, so this cannot pass: "
                    + "; ".join(_blocking)
                    + ". Go back to each one, measure the rebuilt mesh there, and either resolve "
                      "it or report it honestly as unresolved - a FAIL naming the region is a "
                      "correct answer, a PASS over it is not.")
            self.rejection_reasons.append(_why)
            self._mark_no_progress(f"human_flags_blocking:{_signature(_blocking)}")
            return ToolOutcome(content=_why, accepted=False)

        if decision.outcome in (Eligibility.PASS, Eligibility.FAIL):
            # THE ONLY path to a verdict. The application derives PASS/FAIL from these findings.
            self.accepted = decision
            self.accepted_findings = findings
            # Reviewer-authored fields that survive: the report handed to the builder, and its
            # rebuild intent. Neither is a verdict.
            self.accepted_reasoning = str(args.get("reasoning", ""))
            self.accepted_rebuild_required = bool(args.get("rebuild_required", False))
            self.accepted_flag_findings = flag_findings
            self._mark_progress(f"accepted:{sig}")
            return ToolOutcome(content="Findings accepted. Review complete.",
                               terminal=True, payload=decision)

        self.rejections += 1
        self.rejection_reasons.extend(decision.reasons)
        # Progress on a rejection means the DEFICITS moved, or usable evidence arrived - not
        # that the model submitted again.
        moved = (previous is None or deficits.signature() != previous.signature()
                 or self.delta.collected_usable_evidence)
        if moved:
            self._mark_progress(f"deficits:{deficits.signature()}")
        else:
            self._mark_no_progress(f"deficits:{deficits.signature()}")

        # TELL THE REVIEWER WHY. This rejection came from evaluate_eligibility, which composes a
        # reviewer-facing correction_message for exactly this moment - and it was being dropped on
        # the floor, its reasons kept only for the log. What went back instead was the DEFICIT
        # text, and on a rejection whose deficits are clean that text names nothing at all: "The
        # listed axes need a corrected finding" with no axes listed. Nothing in it is actionable,
        # so the model resubmits the same findings, is refused identically, and the review ends at
        # the round limit with no verdict. Six submissions, six rejections, on a mesh that had
        # already passed every other gate in the pipeline.
        #
        # Deficits and eligibility answer different questions - is the submission well-formed, and
        # does it amount to a verdict - so when both have something to say, say both.
        _elig = (decision.correction_message or "").strip()
        if not _elig and decision.reasons:
            _elig = "Rejected: " + "; ".join(decision.reasons)
        if deficits.clean:
            _text = _elig or self.rejection_correction(deficits)
        else:
            _text = self.rejection_correction(deficits)
            if _elig:
                _text = f"{_text}\n{_elig}"
        return ToolOutcome(content=_text, accepted=False)

    def _mark_progress(self, signature: str) -> None:
        self._round_progressed, self._progress_signature = True, signature

    def _mark_no_progress(self, signature: str) -> None:
        self._round_progressed, self._progress_signature = False, signature

    def observe(self, tally: LoopTally) -> ProgressObservation:
        before = self._observed_at or EvidenceSnapshot.of(self.ledger)
        after = EvidenceSnapshot.of(self.ledger)
        step = diff(before, after)
        self._observed_at = after

        if self._round_progressed:                       # a submission moved the deficits
            observation = ProgressObservation(made_progress=True,
                                              signature=self._progress_signature,
                                              detail="eligibility state advanced")
        elif step.collected_usable_evidence:             # navigation actually produced evidence
            self.delta = _merge(self.delta, step)
            observation = ProgressObservation(
                made_progress=True,
                signature=f"evidence:{len(after.usable)}",
                detail=f"{len(step.newly_usable | step.became_usable)} new usable evidence")
        else:
            if step.changed:
                self.delta = _merge(self.delta, step)
            observation = ProgressObservation(
                made_progress=False,
                # The stall identity: WHAT is still unresolved. Two rounds stuck the same way
                # share it; being stuck a new way starts a new streak.
                signature=self._stall_signature(after),
                detail="no new usable evidence and no change in the eligibility deficits")
        self._round_progressed = False
        return observation

    def _stall_signature(self, snapshot: EvidenceSnapshot) -> str:
        deficits = self.last_deficits
        parts = [deficits.signature() if deficits else "pre-submission",
                 self.last_findings_sig or "no-submission",
                 str(len(snapshot.usable))]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]

    # corrections
    def rejection_correction(self, deficits: AxisDeficits) -> str:
        return self._correction_body(deficits, unchanged=not self.last_submission_changed)

    def correction(self, stage: LoopStage, tally: LoopTally,
                   observation: ProgressObservation) -> str | None:
        if observation.made_progress:
            return None
        deficits = self.last_deficits
        if deficits is None:
            return None
        return self._correction_body(deficits, unchanged=True, tally=tally, escalated=True)

    def _correction_body(self, deficits: AxisDeficits, *, unchanged: bool,
                         tally: LoopTally | None = None, escalated: bool = False) -> str:
        used = tally.rounds if tally else None
        remaining = tally.remaining_rounds(self.limits_) if tally else None
        lines = ["Submission rejected."]
        if deficits.missing:
            lines.append(f"Missing axes: {', '.join(deficits.missing)}")
        if deficits.ungrounded:
            lines.append(f"Ungrounded axes: {', '.join(deficits.ungrounded)}")
        if deficits.duplicate:
            lines.append(f"Duplicate axes: {', '.join(deficits.duplicate)}")
        if deficits.invalid_evidence:
            lines.append(f"Unknown evidence ids: {', '.join(deficits.invalid_evidence)}")
        needs_evidence = bool(deficits.missing or deficits.ungrounded)
        lines.append("New usable evidence is REQUIRED for the listed axes."
                     if needs_evidence else
                     "The listed axes need a corrected finding, not more evidence.")
        if used is not None:
            lines.append(f"Rounds used: {used}. Rounds remaining: {remaining}.")
        if unchanged:
            lines.append("Your previous submission was materially unchanged. Do not resubmit "
                         "unchanged findings - the same submission is rejected the same way.")
        if escalated:
            lines.append("These deficits have not changed. Collect usable evidence for the "
                         "listed axes now, or this review ends without a verdict.")
        return "\n".join(lines)

    def _malformed_correction(self) -> str:
        return ("Your tool arguments could not be parsed as JSON, so nothing was recorded. "
                f"Call {SUBMIT_FINDINGS} again with valid JSON arguments.")

    def _malformed_findings_correction(self, problems: tuple[str, ...]) -> str:
        return "\n".join([
            "Submission rejected - the findings payload is malformed and was NOT evaluated.",
            *problems,
            "No axis was recorded as passing or failing. Every finding needs axis_key, finding, "
            "evidence_ids and a literal true/false `passed`, and no other fields.",
        ])

    def before_round(self, tally: LoopTally, messages: list[dict]) -> None:
        pass

    def forced_tool(self, tally: LoopTally) -> str | None:
        return None

    def on_plaintext(self, tally: LoopTally) -> RoundDecision:
        remaining = tally.remaining_rounds(self.limits_)
        return RoundDecision(
            message=("Respond with a tool call: inspect the mesh, or submit_findings. Plain text "
                     f"is not recorded. Rounds remaining: {remaining}."),
            complete=False)

    def is_supersession(self, exc: BaseException) -> bool:
        from meshpipeline.application.execution_fence import StaleWorkerFenced
        return isinstance(exc, StaleWorkerFenced)

    async def close_out(self, tally: LoopTally) -> ToolOutcome | None:
        return None

    # diagnostics
    def extension(self) -> ReviewRunExtension:
        deficits = self.last_deficits or AxisDeficits(missing=self.required_axes)
        snapshot = self._snapshot or EvidenceSnapshot.of(self.ledger)
        counts = self.delta.counts()
        return ReviewRunExtension(
            required_axes=self.required_axes,
            covered_axes=deficits.covered,
            missing_axes=deficits.missing,
            duplicate_axes=deficits.duplicate,
            ungrounded_axes=deficits.ungrounded,
            invalid_evidence_refs=deficits.invalid_evidence,
            usable_evidence_count=len(snapshot.usable),
            unusable_evidence_count=len(snapshot.unusable),
            newly_usable_evidence=counts["newly_usable"],
            newly_unusable_evidence=counts["newly_unusable"],
            became_usable_evidence=counts["became_usable"],
            repeated_evidence=counts["repeated"],
            submissions=self.submissions,
            malformed_submissions=self.malformed_submissions,
            eligibility_rejections=self.rejections,
            rejection_reasons=tuple(self.rejection_reasons),
            last_submission_changed=self.last_submission_changed,
            new_evidence_since_last_submission=self.delta.collected_usable_evidence,
            accepted_verdict=(self.accepted.verdict if self.accepted else None))


def _merge(a: EvidenceDelta, b: EvidenceDelta) -> EvidenceDelta:
    return EvidenceDelta(
        newly_usable=a.newly_usable | b.newly_usable,
        newly_unusable=a.newly_unusable | b.newly_unusable,
        became_usable=a.became_usable | b.became_usable,
        became_unusable=a.became_unusable | b.became_unusable,
        repeated=a.repeated | b.repeated)


__all__ = [
    "MARKER_EVIDENCE_MISSING",
    "REVIEWER_NO_PROGRESS_THRESHOLD",
    "MARKER_EXHAUSTED",
    "SUBMIT_FINDINGS",
    "AxisDeficits",
    "ReviewLoopPolicy",
    "compute_deficits",
    "normalize_findings",
]
