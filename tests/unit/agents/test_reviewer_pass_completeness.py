# Responsibility: Verify an exhausted review reads as review evidence, not a provider outage and not a mesh verdict.
from __future__ import annotations

from meshpipeline.errors import (
    FailureClass,
    classify_api_failure,
    failed_reason_for,
    user_message_for,
)


# the truthful non-verdict exit (evidence/render/exhaustion, never provider-down)
def test_the_exhaustion_marker_classifies_as_review_evidence_not_provider_downtime():
    fc = classify_api_failure("reviewer_evidence_missing")
    assert fc is FailureClass.REVIEW_EVIDENCE_MISSING
    assert fc is not FailureClass.PROVIDER_DOWN
    assert fc is not FailureClass.PROVIDER_TRANSIENT


def test_the_marker_classifies_the_same_wrapped_or_bare():
    assert (classify_api_failure("<<API_FAILURE:reviewer_evidence_missing>>")
            is classify_api_failure("reviewer_evidence_missing")
            is FailureClass.REVIEW_EVIDENCE_MISSING)


def test_the_user_is_told_the_real_reason_not_an_ai_outage():
    msg = user_message_for(FailureClass.REVIEW_EVIDENCE_MISSING)
    assert "quality criteria" in msg and "verify" in msg
    for lie in ("AI service", "unavailable", "temporarily experiencing"):
        assert lie not in msg, f"the message blames {lie!r} for an evidence problem"


def test_it_is_a_system_failure_not_a_mesh_verdict():
    assert FailureClass.REVIEW_EVIDENCE_MISSING.is_system
    assert not FailureClass.REVIEW_EVIDENCE_MISSING.is_retryable


def test_it_persists_through_an_existing_db_value_with_no_migration():
    assert failed_reason_for(FailureClass.REVIEW_EVIDENCE_MISSING) == "api_failure"
    from meshpipeline.persistence.models import FailedReason
    FailedReason("api_failure")                     # the value already exists


def test_the_renderer_and_provider_paths_are_classified_truthfully():
    assert classify_api_failure("reviewer_render_unavailable") is FailureClass.REVIEW_EVIDENCE_MISSING
    assert classify_api_failure("reviewer_evidence_missing") is FailureClass.REVIEW_EVIDENCE_MISSING
    assert classify_api_failure("reviewer_exhausted") is FailureClass.REVIEW_EVIDENCE_MISSING
    assert classify_api_failure("<<API_FAILURE:builder_server_error>>") is FailureClass.PROVIDER_DOWN
    assert classify_api_failure("<<API_FAILURE:builder_rate_limit>>") is FailureClass.PROVIDER_TRANSIENT
    assert classify_api_failure("<<API_FAILURE:intake_unavailable>>") is FailureClass.PROVIDER_TRANSIENT


def test_a_non_transient_marker_is_classified_non_retryable():
    fc = classify_api_failure("<<API_FAILURE:builder_non_transient>>")
    assert fc is FailureClass.PROVIDER_DOWN
    assert not fc.is_retryable


def test_coverage_is_required_for_a_verdict_of_EITHER_polarity():
    from meshpipeline.agents.reviewer.eligibility import (
        AxisFinding,
        Eligibility,
        evaluate_eligibility,
    )
    from meshpipeline.contracts.evidence_ledger import EvidenceLedger, EvidenceStatus
    from meshpipeline.contracts.review_outcome import ReviewVerdict

    class _Ax:
        def __init__(self, name):
            self.name, self.requires = name, ()

    class _Plan:
        engine = purpose = "x"
        required_gate_keys = frozenset({"rc"})
        required_metric_keys = required_render_artifacts = frozenset()
        required_targets = ()
        requires_render = True
        axes = (_Ax("a"), _Ax("b"))

    L = EvidenceLedger()
    g = L.add_gate("rc", EvidenceStatus.FAIL, "nonzero", "executor")  # one deterministic defect
    plan = _Plan()
    # An ALL-PASSING submission cannot stand over a failed hard gate.
    all_pass = evaluate_eligibility(
        plan, L, (AxisFinding("a", "x", (g,)), AxisFinding("b", "y", (g,))))
    assert all_pass.outcome is Eligibility.REJECT and all_pass.verdict is None

    # Partial coverage is rejected whatever the judgement - a verdict of EITHER polarity is a
    # statement about the WHOLE mesh, so every required axis must be answered. (Before the
    # one-rule cutover a FAIL was accepted on partial coverage; that leniency is gone.)
    partial_pass = evaluate_eligibility(plan, L, (AxisFinding("a", "x", (g,)),))
    assert partial_pass.outcome is Eligibility.REJECT and partial_pass.verdict is None
    partial_fail = evaluate_eligibility(
        plan, L, (AxisFinding("a", "gate failed", (g,), passed=False),))
    assert partial_fail.outcome is Eligibility.REJECT and partial_fail.verdict is None
    assert any("axis 'b' has no finding" in r for r in partial_fail.reasons)

    # Complete coverage with the defect named on its axis IS accepted - and the failed gate
    # that grounds it is no longer also a reason to refuse it.
    full_fail = evaluate_eligibility(plan, L, (
        AxisFinding("a", "gate failed", (g,), passed=False),
        AxisFinding("b", "otherwise fine", (g,), passed=True)))
    assert full_fail.outcome is Eligibility.FAIL
    assert full_fail.verdict is ReviewVerdict.failed
