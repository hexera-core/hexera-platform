# Responsibility: Decide whether a submission may be accepted, and derive its verdict when it may.
# Owns: the eligibility decision, per-axis findings, outstanding target obligations, and plan validation.
# Boundaries: a verdict requires the evidence the engine and purpose declared.
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from meshpipeline.contracts.evidence_ledger import (
    EvidenceLedger,
    HardGateEvidence,
    InspectionTargetRef,
    MetricEvidence,
    TargetInspectionEvidence,
)
from meshpipeline.contracts.review_evidence import (
    HardGateRequirement,
    MetricRequirement,
    RenderTargetRequirement,
    RenderViewRequirement,
    SpatialInspectionRequirement,
    TargetKind,
)
from meshpipeline.contracts.review_outcome import ReviewVerdict


@dataclass(frozen=True)
class AxisFinding:

    axis_key: str
    finding: str
    evidence_ids: tuple[str, ...] = ()
    # The reviewer's judgement FOR THIS AXIS - the same word the persisted per-axis record and
    # the ledger's gate/metric evidence already use. It is NOT an aggregate verdict: a verdict
    # covers the whole mesh and exists only once eligibility accepts the submission. A `False`
    # must be GROUNDED, or the submission is rejected.
    passed: bool = True


class Eligibility(str, Enum):

    PASS = "pass"                    # noqa: S105 - an outcome value, not a secret; accepted, clean
    FAIL = "fail"                    # accepted, with a grounded defect
    REJECT = "reject"                # not accepted - correct the findings and retry


@dataclass(frozen=True)
class EligibilityDecision:

    outcome: Eligibility
    verdict: ReviewVerdict | None = None     # present iff outcome is PASS or FAIL
    reasons: tuple[str, ...] = ()            # machine-readable unmet obligations
    correction_message: str = ""             # bounded reviewer-facing message for REJECT

    def __post_init__(self) -> None:
        if self.accepted != (self.verdict is not None):
            raise ValueError(
                "a verdict exists exactly when eligibility accepts the submission; got "
                f"outcome={self.outcome!r} verdict={self.verdict!r}")
        if self.accepted:
            expected = (ReviewVerdict.passed if self.outcome is Eligibility.PASS
                        else ReviewVerdict.failed)
            if self.verdict is not expected:
                raise ValueError(f"outcome {self.outcome!r} cannot carry verdict {self.verdict!r}")

    @property
    def accepted(self) -> bool:
        return self.outcome in (Eligibility.PASS, Eligibility.FAIL)


# obligations derived from the plan (axes' typed requires + plan-level requirements)
@dataclass(frozen=True)
class _Obligations:
    gate_keys: frozenset[str]
    metric_keys: frozenset[str]
    view_ids: frozenset[str]
    exact_targets: tuple[InspectionTargetRef, ...]
    wildcard_kinds: frozenset[TargetKind]


def _is_wildcard(selector: str) -> bool:
    return selector == "*" or selector.endswith(":*")


def _derive_obligations(plan) -> _Obligations:
    gate_keys = set(plan.required_gate_keys)
    metric_keys = set(plan.required_metric_keys)
    view_ids: set[str] = set()
    exact: dict[str, InspectionTargetRef] = {}
    wildcard: set[TargetKind] = set()

    for ax in plan.axes:
        for req in getattr(ax, "requires", ()):
            if isinstance(req, HardGateRequirement):
                gate_keys.add(req.key)
            elif isinstance(req, MetricRequirement):
                metric_keys.add(req.key)
            elif isinstance(req, RenderViewRequirement):
                view_ids.add(req.view_id)
            elif isinstance(req, RenderTargetRequirement):
                if _is_wildcard(req.selector):
                    wildcard.add(req.kind)
                else:
                    exact[req.selector] = InspectionTargetRef(req.kind, req.selector)
            elif isinstance(req, SpatialInspectionRequirement):
                wildcard.add(req.kind)

    # The plan's required inspection targets (required=True) are the coverage floor. A wildcard id
    # ("group:*") is a kind obligation; a concrete id is an exact obligation.
    for t in plan.required_targets:
        if _is_wildcard(t.target_id):
            wildcard.add(t.kind)
        else:
            exact[t.target_id] = InspectionTargetRef(t.kind, t.target_id)

    return _Obligations(
        gate_keys=frozenset(gate_keys), metric_keys=frozenset(metric_keys),
        view_ids=frozenset(view_ids), exact_targets=tuple(exact.values()),
        wildcard_kinds=frozenset(wildcard))


# deterministic-evidence completeness (Part 6: before the provider may conclude)
def deterministic_evidence_complete(plan, ledger: EvidenceLedger) -> tuple[bool, tuple[str, ...]]:
    missing: list[str] = []
    ob = _derive_obligations(plan)
    for key in sorted(ob.gate_keys):
        if ledger.usable_gate(key) is None:
            missing.append(f"gate:{key}")
    for key in sorted(ob.metric_keys):
        if ledger.usable_metric(key) is None:
            missing.append(f"metric:{key}")
    return (not missing, tuple(missing))


# requirement satisfaction (for a single axis finding's citations)
def _citation_satisfies(req, records) -> bool:
    for r in records:
        if isinstance(req, HardGateRequirement):
            if isinstance(r, HardGateEvidence) and r.gate_key == req.key:
                return True
        elif isinstance(req, MetricRequirement):
            if isinstance(r, MetricEvidence) and r.metric_key == req.key:
                return True
        elif isinstance(req, RenderViewRequirement):
            from meshpipeline.contracts.evidence_ledger import RenderViewEvidence
            if isinstance(r, RenderViewEvidence) and r.view_id == req.view_id:
                return True
        elif isinstance(req, RenderTargetRequirement):
            if isinstance(r, TargetInspectionEvidence) and r.target.kind is req.kind:
                if _is_wildcard(req.selector) or r.target.target_id == req.selector:
                    return True
        elif isinstance(req, SpatialInspectionRequirement):
            if isinstance(r, TargetInspectionEvidence) and r.target.kind is req.kind:
                return True
    return False


def _usable_citations(finding: AxisFinding, ledger: EvidenceLedger) -> tuple[list, tuple[str, ...]]:
    records: list = []
    problems: list[str] = []
    for eid in finding.evidence_ids:
        rec = ledger.get(eid)
        if rec is None:
            problems.append(f"unknown evidence id {eid}")
        elif not getattr(rec, "usable", False):
            problems.append(f"unusable evidence {eid}")
        else:
            records.append(rec)
    return records, tuple(problems)


def _req_applicable(req, expected_kinds) -> bool:
    if expected_kinds is None:
        return True
    if isinstance(req, (RenderTargetRequirement, SpatialInspectionRequirement)):
        return req.kind in expected_kinds
    return True


# A refusal the recipient cannot act on is a loop, not a gate. The reviewer that reads "does not
# satisfy required MetricRequirement" is told a rule name: not WHICH metric, and not that the
# answer is an id already on file rather than another render. Observed in a real review: the axes
# whose deficits named their targets ("region 'nearwall_x' has no usable inspection") were all
# repaired in one round, while the axis whose deficit named only a class name was retried - with
# re-run inspections and a reworded number - until the round budget ran out and the job failed.
# So every branch here names the thing required, and, where the evidence already exists, the id.
def _or_cite(have) -> str:
    """Point at evidence already on file, or ask for it - never ask for what is already there."""
    ids = [h.evidence_id for h in have if h is not None]
    if not ids:
        return " - produce that inspection, then cite the id it returns"
    if len(ids) == 1:
        return f" - cite {ids[0]}, which is already on file"
    return f" - cite one of {', '.join(ids)}, which are already on file"


def _requirement_hint(req, ledger: EvidenceLedger) -> str:
    if isinstance(req, HardGateRequirement):
        gate = ledger.usable_gate(req.key)
        if gate is not None:
            return (f"gate '{req.key}' - cite {gate.evidence_id}, which is already on file; "
                    "no tool call produces it")
        return f"gate '{req.key}', which is not on file"
    if isinstance(req, MetricRequirement):
        metric = ledger.usable_metric(req.key)
        if metric is not None:
            return (f"metric '{req.key}' - cite {metric.evidence_id}, which is already on file. "
                    "It is a MEASUREMENT: no render, slice or patch inspection can stand in for "
                    "it, and quoting its number in the prose is not citing it")
        return f"metric '{req.key}', which is not on file"
    if isinstance(req, RenderViewRequirement):
        view = ledger.usable_view(req.view_id)
        if view is not None:
            return f"the '{req.view_id}' view - cite {view.evidence_id}, which is already on file"
        return f"the '{req.view_id}' view, which is not on file"
    # An inspection the ledger already holds is the commonest case here: the axis cited the wrong
    # evidence, not none. Telling it to "produce that inspection" then sends it to re-run a tool it
    # has already run - which is the retry loop this function exists to break, not a repair. So
    # look first, and only ask for new work when there is genuinely nothing to cite.
    if isinstance(req, RenderTargetRequirement):
        if _is_wildcard(req.selector):
            return f"an inspection of any {req.kind.value}{_or_cite(ledger.usable_inspections_of_kind(req.kind))}"
        exact = ledger.usable_inspection_of_target(InspectionTargetRef(req.kind, req.selector))
        return (f"an inspection of {req.kind.value} '{req.selector}'"
                f"{_or_cite([exact] if exact is not None else [])}")
    if isinstance(req, SpatialInspectionRequirement):
        return f"an inspection of any {req.kind.value}{_or_cite(ledger.usable_inspections_of_kind(req.kind))}"
    return type(req).__name__


def _axis_finding_suitable(ax, finding: AxisFinding, ledger: EvidenceLedger,
                           expected_kinds=None) -> tuple[bool, str]:
    if not finding.finding.strip():
        return False, f"axis '{finding.axis_key}' finding is blank"
    records, problems = _usable_citations(finding, ledger)
    if problems:
        return False, f"axis '{finding.axis_key}': " + "; ".join(problems)
    if not records:
        return False, f"axis '{finding.axis_key}' cites no usable evidence"
    for req in tuple(getattr(ax, "requires", ())):
        if not _req_applicable(req, expected_kinds):
            continue
        if not _citation_satisfies(req, records):
            return False, (f"axis '{finding.axis_key}' does not cite "
                           f"{_requirement_hint(req, ledger)}")
    return True, ""


def _check_obligation(o, ledger: EvidenceLedger) -> list[str]:
    reasons: list[str] = []
    prov = f" ({o.provenance})" if o.provenance else ""
    if o.exact_ids:
        for tid in o.exact_ids:
            if ledger.usable_inspection_of_target(InspectionTargetRef(o.kind, tid)) is None:
                reasons.append(f"expected {o.kind.value} target '{tid}' has no usable inspection{prov}")
        return reasons
    have = len(ledger.usable_inspections_of_kind(o.kind))
    need = max(o.min_count, 1)
    if have < need:
        reasons.append(f"expected {need} {o.kind.value} inspection(s), have {have}{prov}")
    return reasons


def missing_target_obligations(obligations, discovered_ids: dict) -> list[str]:
    problems: list[str] = []
    for o in obligations:
        found = set(discovered_ids.get(o.kind, ()) or ())
        prov = f" ({o.provenance})" if o.provenance else ""
        if o.exact_ids:
            for tid in o.exact_ids:
                if tid not in found:
                    problems.append(f"expected {o.kind.value} '{tid}' was not produced{prov}")
        else:
            need = max(o.min_count, 1)
            if len(found) < need:
                problems.append(
                    f"expected {need} {o.kind.value} target(s), the mesh produced {len(found)}{prov}")
    return problems


# the ONE eligibility rule
# Deterministic eligibility evaluates the typed per-axis findings DIRECTLY. There is no declared
# aggregate verdict to dispatch on: the reviewer states, per required axis, whether the mesh
# passed it, and this decides whether that submission may be accepted at all. Only when it is
# accepted does a verdict exist, and it is derived here from the same findings - never asked of
# the model, never assembled from a string.


def _grounds_a_defect(records) -> bool:
    for r in records:
        if isinstance(r, HardGateEvidence) and not r.passed:
            return True
        if isinstance(r, MetricEvidence) and not r.passed:
            return True
        if isinstance(r, TargetInspectionEvidence) and r.usable:
            return True
    return False


def _defect_grounded(finding: AxisFinding, ledger: EvidenceLedger) -> bool:
    records, problems = _usable_citations(finding, ledger)
    if problems or not records:
        return False
    return _grounds_a_defect(records)


def _evidence_presence_reasons(plan, ledger: EvidenceLedger,
                               obligations: tuple | None) -> list[str]:
    reasons: list[str] = []
    ob = _derive_obligations(plan)
    for key in sorted(ob.gate_keys):
        if ledger.usable_gate(key) is None:
            reasons.append(f"required gate '{key}' has no usable evidence")
    for key in sorted(ob.metric_keys):
        if ledger.usable_metric(key) is None:
            reasons.append(f"required metric '{key}' has no usable evidence")
    for vid in sorted(ob.view_ids):
        if ledger.usable_view(vid) is None:
            reasons.append(f"required view '{vid}' has no usable evidence")
    for ref in ob.exact_targets:
        if ledger.usable_inspection_of_target(ref) is None:
            reasons.append(f"required target '{ref.target_id}' has no usable target-specific "
                           "evidence")
    if obligations is None:
        # ledger-only unit behaviour: enforce the plan's own wildcard kinds at >= 1
        for kind in sorted(ob.wildcard_kinds, key=lambda k: k.value):
            if not ledger.usable_inspections_of_kind(kind):
                reasons.append(f"required {kind.value} inspection has no usable evidence")
    else:
        # engine-RESOLVED obligations are authoritative - checked at their own strength, so
        # partial loss is caught and an unproduced target is missing, never waived.
        for o in obligations:
            reasons.extend(_check_obligation(o, ledger))
    return reasons


def _objective_defects(plan, ledger: EvidenceLedger) -> list[str]:
    out: list[str] = []
    ob = _derive_obligations(plan)
    for key in sorted(ob.gate_keys):
        g = ledger.usable_gate(key)
        if g is not None and not g.passed:
            out.append(f"hard gate '{key}' failed")
    for key in sorted(ob.metric_keys):
        m = ledger.usable_metric(key)
        if m is not None and (m.status.value == "fail" or m.acceptable is False):
            out.append(f"required metric '{key}' is not acceptable")
    return out


def evaluate_eligibility(plan, ledger: EvidenceLedger, findings: tuple[AxisFinding, ...],
                         *, lifecycle_ok: bool = True,
                         obligations: tuple | None = None) -> EligibilityDecision:
    reasons: list[str] = []
    axes_by_name = {ax.name: ax for ax in plan.axes}
    obligated_kinds = (frozenset(o.kind for o in obligations)
                       if obligations is not None else None)

    # 1. coverage - exactly one finding per required axis
    seen: dict[str, int] = {}
    for f in findings:
        seen[f.axis_key] = seen.get(f.axis_key, 0) + 1
    for name in axes_by_name:
        n = seen.get(name, 0)
        if n == 0:
            reasons.append(f"axis '{name}' has no finding")
        elif n > 1:
            reasons.append(f"axis '{name}' has {n} findings (exactly one required)")

    # 2. each finding is for a known axis, nonblank, and grounded in usable evidence
    for f in findings:
        ax = axes_by_name.get(f.axis_key)
        if ax is None:
            reasons.append(f"finding for unknown axis '{f.axis_key}'")
            continue
        ok, why = _axis_finding_suitable(ax, f, ledger, obligated_kinds)
        if not ok:
            reasons.append(why)

    # 3. the evidence a verdict of EITHER polarity requires must exist
    reasons.extend(_evidence_presence_reasons(plan, ledger, obligations))

    # 4. a NOT-passed axis must be grounded - the same bar an ungrounded FAIL always faced
    failed_axes = [f for f in findings if not f.passed and f.axis_key in axes_by_name]
    for f in failed_axes:
        if not _defect_grounded(f, ledger):
            reasons.append(f"axis '{f.axis_key}' is marked not passed but cites no evidence that "
                           "grounds a defect (a failed gate or metric, or a target-specific "
                           "inspection you produced)")

    # 5. an all-passing submission may not contradict a measured failure
    if not failed_axes:
        reasons.extend(_objective_defects(plan, ledger))

    # 6. conflicts and lifecycle
    for c in ledger.unresolved_conflicts():
        reasons.append(f"unresolved evidence conflict: {c.summary}")
    if not lifecycle_ok:
        reasons.append("renderer/evidence lifecycle did not complete")

    if reasons:
        return EligibilityDecision(
            outcome=Eligibility.REJECT, verdict=None, reasons=tuple(reasons),
            correction_message=_correction(reasons))

    # ACCEPTED. The verdict is derived from the same findings that were just accepted, so the
    # per-axis record and the verdict can never disagree.
    verdict = ReviewVerdict.failed if failed_axes else ReviewVerdict.passed
    return EligibilityDecision(
        outcome=Eligibility.FAIL if failed_axes else Eligibility.PASS, verdict=verdict)


def _correction(reasons: list[str]) -> str:
    return ("SUBMISSION NOT ACCEPTED - " + "; ".join(reasons) +
            ". Gather the missing evidence and cite it, or correct the findings. Do not describe "
            "evidence you have not produced, and do not resubmit unchanged findings.")


# plan validation (Part 3: reject a malformed plan before the provider is invoked)
def validate_plan(spec, plan) -> tuple[bool, tuple[str, ...]]:
    problems: list[str] = []
    declared_kinds = {t.kind for t in spec.inspection_targets}
    declared_artifacts = {a.artifact_key for a in spec.render_artifacts}
    for ax in plan.axes:
        for req in getattr(ax, "requires", ()):
            if isinstance(req, (RenderTargetRequirement, SpatialInspectionRequirement)):
                if req.kind not in declared_kinds:
                    problems.append(f"axis '{ax.name}' requires {req.kind.value} targets but the "
                                    "engine declares no inspection target of that kind")
    for artifact_key in sorted(plan.required_render_artifacts):
        if artifact_key not in declared_artifacts:
            problems.append(f"plan requires render artifact '{artifact_key}' the engine does not "
                            "declare")
    return (not problems, tuple(problems))


__all__ = [
    "AxisFinding",
    "Eligibility",
    "EligibilityDecision",
    "deterministic_evidence_complete",
    "evaluate_eligibility",
    "validate_plan",
    "missing_target_obligations",
]
