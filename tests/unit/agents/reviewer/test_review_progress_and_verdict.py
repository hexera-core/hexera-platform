# Responsibility: Verify what counts as new evidence, what blocks a verdict, and what a rejection must name.
from __future__ import annotations

import types

import pytest

from meshpipeline.agents.reviewer.eligibility import AxisFinding
from meshpipeline.agents.reviewer.evidence_delta import EvidenceSnapshot, diff
from meshpipeline.agents.reviewer.loop_policy import (
    REVIEWER_NO_PROGRESS_THRESHOLD,
    AxisDeficits,
    compute_deficits,
    normalize_findings,
)
from meshpipeline.contracts.evidence_ledger import (
    EvidenceLedger,
    EvidenceStatus,
    InspectionTargetRef,
    TargetKind,
)
from meshpipeline.contracts.review_outcome import ReviewVerdict

AXES = ("surface_capture", "wake_resolution")


def _plan(*names):
    return types.SimpleNamespace(
        axes=tuple(types.SimpleNamespace(name=n, owner="engine:snappy", requires=())
                   for n in (names or AXES)),
        engine="snappy", purpose="external_cfd",
        required_gate_keys=frozenset(), required_metric_keys=frozenset(),
        required_render_artifacts=frozenset(), required_targets=())


# the explicit evidence delta
def test_new_usable_evidence_is_distinguished_from_everything_else():
    L = EvidenceLedger()
    before = EvidenceSnapshot.of(L)
    good = L.add_render_view("iso", "k", "open", image_ok=True)
    d = diff(before, EvidenceSnapshot.of(L))
    assert d.newly_usable == {good} and d.collected_usable_evidence is True


def test_unusable_evidence_is_not_new_evidence():
    L = EvidenceLedger()
    before = EvidenceSnapshot.of(L)
    bad = L.add_validation("render produced no usable image", "zoom")
    d = diff(before, EvidenceSnapshot.of(L))
    assert d.newly_unusable == {bad}
    assert d.newly_usable == frozenset() and d.collected_usable_evidence is False
    assert len(L) == 1, "the ledger DID grow - which is exactly why length cannot decide progress"


def test_repeating_existing_evidence_is_not_new_evidence():
    L = EvidenceLedger()
    first = L.add_render_view("iso", "k", "open", image_ok=True)
    before = EvidenceSnapshot.of(L)
    d = diff(before, EvidenceSnapshot.of(L), cited=frozenset({first}))
    assert d.repeated == {first}
    assert d.collected_usable_evidence is False and d.changed is False


def test_evidence_replacement_is_visible_as_both_halves():
    L = EvidenceLedger()
    ref = InspectionTargetRef(TargetKind.PATCH, "patch:wing")
    one = L.add_target_inspection(ref, "toggle_patch", image_ok=True, covered=True)
    before = EvidenceSnapshot.of(L)
    two = L.add_target_inspection(ref, "go_to_coordinates", image_ok=True, covered=True)
    d = diff(before, EvidenceSnapshot.of(L), cited=frozenset({one}))
    assert d.newly_usable == {two} and d.repeated == {one}
    assert d.collected_usable_evidence is True


def test_a_snapshot_separates_usable_from_unusable():
    L = EvidenceLedger()
    ok = L.add_render_view("iso", "k", "open", image_ok=True)
    bad = L.add_validation("nothing rendered", "zoom")
    snap = EvidenceSnapshot.of(L)
    assert snap.usable == {ok} and snap.unusable == {bad}
    assert snap.all_ids == {ok, bad}


def test_no_evidence_change_is_reported_as_no_change():
    L = EvidenceLedger()
    L.add_render_view("iso", "k", "open", image_ok=True)
    snap = EvidenceSnapshot.of(L)
    d = diff(snap, snap)
    assert not d.changed and not d.collected_usable_evidence
    assert d.counts() == {"newly_usable": 0, "newly_unusable": 0, "became_usable": 0,
                          "became_unusable": 0, "repeated": 0}


# deficits and stall identity
def _usable_view(L):
    return L.add_render_view("iso", "k", "open", image_ok=True)


def test_missing_axes_are_the_ones_with_no_finding():
    L = EvidenceLedger()
    d = compute_deficits(AXES, (AxisFinding("surface_capture", "ok", (_usable_view(L),)),), L)
    assert d.missing == ("wake_resolution",) and d.covered == ("surface_capture",)


def test_a_duplicate_axis_is_named():
    L = EvidenceLedger()
    e = _usable_view(L)
    d = compute_deficits(AXES, (AxisFinding("surface_capture", "a", (e,)),
                                AxisFinding("surface_capture", "b", (e,)),
                                AxisFinding("wake_resolution", "c", (e,))), L)
    assert d.duplicate == ("surface_capture",)


def test_an_axis_citing_nothing_usable_is_ungrounded():
    L = EvidenceLedger()
    bad = L.add_validation("no image", "zoom")
    d = compute_deficits(AXES, (AxisFinding("surface_capture", "x", (bad,)),), L)
    assert "surface_capture" in d.ungrounded


def test_an_unknown_evidence_id_is_named():
    L = EvidenceLedger()
    d = compute_deficits(AXES, (AxisFinding("surface_capture", "x", ("t-999",)),), L)
    assert d.invalid_evidence == ("t-999",)


def test_the_deficit_signature_is_stable_and_discriminating():
    a = AxisDeficits(missing=("x",), ungrounded=("y",))
    assert a.signature() == AxisDeficits(missing=("x",), ungrounded=("y",)).signature()
    assert a.signature() != AxisDeficits(missing=("x",)).signature()


def test_findings_are_identified_by_claim_not_prose():
    same_a = (AxisFinding("a", "the wing looks fine", ("e1",)),)
    same_b = (AxisFinding("a", "completely different wording", ("e1",)),)
    other = (AxisFinding("a", "the wing looks fine", ("e2",)),)
    assert normalize_findings(same_a) == normalize_findings(same_b)
    assert normalize_findings(same_a) != normalize_findings(other)


def test_the_stall_threshold_matches_the_builders_corrective_repeat_point():
    from meshpipeline.agents.builder.loop_policy import BUILDER_NO_PROGRESS_THRESHOLD
    assert BUILDER_NO_PROGRESS_THRESHOLD == 4                        # builder ABORTS here
    assert REVIEWER_NO_PROGRESS_THRESHOLD == BUILDER_NO_PROGRESS_THRESHOLD - 1
    assert REVIEWER_NO_PROGRESS_THRESHOLD == 3


# the application-derived verdict
def _accept(L, *findings, plan=None):
    from meshpipeline.agents.reviewer.eligibility import evaluate_eligibility
    return evaluate_eligibility(plan or _plan(), L, tuple(findings))


def _clean_findings(L, *names):
    e = _usable_view(L)
    return tuple(AxisFinding(n, f"{n} ok", (e,), passed=True) for n in names)


def test_accepted_clean_findings_derive_pass():
    L = EvidenceLedger()
    d = _accept(L, *_clean_findings(L, *AXES))
    assert d.accepted and d.verdict is ReviewVerdict.passed


def test_accepted_defect_findings_derive_fail():
    L = EvidenceLedger()
    gate = L.add_gate("mesh_quality", EvidenceStatus.FAIL, "skew too high", "executor")
    d = _accept(L, AxisFinding("surface_capture", "skew", (gate,), passed=False),
                *_clean_findings(L, "wake_resolution"))
    assert d.accepted and d.verdict is ReviewVerdict.failed


def test_a_thoroughly_inspected_passing_axis_is_not_a_defect():
    L = EvidenceLedger()
    seen = L.add_target_inspection(InspectionTargetRef(TargetKind.PATCH, "patch:wing"),
                                   "toggle_patch", image_ok=True, covered=True)
    d = _accept(L, *(AxisFinding(n, "inspected closely, clean", (seen,), passed=True)
                     for n in AXES))
    assert d.accepted and d.verdict is ReviewVerdict.passed


def test_a_failed_axis_without_usable_defect_evidence_is_rejected():
    L = EvidenceLedger()
    nothing = L.add_validation("no image produced", "zoom")
    d = _accept(L, AxisFinding("surface_capture", "looks bad", (nothing,), passed=False),
                *_clean_findings(L, "wake_resolution"))
    assert not d.accepted and d.verdict is None
    assert any("grounds a defect" in r for r in d.reasons)


def test_a_missing_axis_produces_no_verdict():
    L = EvidenceLedger()
    d = _accept(L, *_clean_findings(L, "surface_capture"))
    assert not d.accepted and d.verdict is None
    assert any("wake_resolution" in r and "no finding" in r for r in d.reasons)


def test_a_duplicate_axis_produces_no_verdict():
    L = EvidenceLedger()
    e = _usable_view(L)
    d = _accept(L, AxisFinding("surface_capture", "a", (e,)),
                AxisFinding("surface_capture", "b", (e,)),
                AxisFinding("wake_resolution", "c", (e,)))
    assert not d.accepted and d.verdict is None
    assert any("exactly one required" in r for r in d.reasons)


def test_an_unknown_evidence_reference_produces_no_verdict():
    L = EvidenceLedger()
    d = _accept(L, AxisFinding("surface_capture", "x", ("t-999",)),
                *_clean_findings(L, "wake_resolution"))
    assert not d.accepted and d.verdict is None


def test_an_all_passing_submission_cannot_contradict_a_measured_failure():
    L = EvidenceLedger()
    L.add_gate("rc", EvidenceStatus.FAIL, "failed", "executor")
    plan = types.SimpleNamespace(
        axes=tuple(types.SimpleNamespace(name=n, owner="e", requires=()) for n in AXES),
        engine="snappy", purpose="external_cfd",
        required_gate_keys=frozenset({"rc"}), required_metric_keys=frozenset(),
        required_render_artifacts=frozenset(), required_targets=())
    d = _accept(L, *_clean_findings(L, *AXES), plan=plan)
    assert not d.accepted and d.verdict is None
    assert any("hard gate 'rc' failed" in r for r in d.reasons)


def test_that_same_failed_gate_can_GROUND_a_fail():
    L = EvidenceLedger()
    g = L.add_gate("rc", EvidenceStatus.FAIL, "failed", "executor")
    plan = types.SimpleNamespace(
        axes=tuple(types.SimpleNamespace(name=n, owner="e", requires=()) for n in AXES),
        engine="snappy", purpose="external_cfd",
        required_gate_keys=frozenset({"rc"}), required_metric_keys=frozenset(),
        required_render_artifacts=frozenset(), required_targets=())
    d = _accept(L, AxisFinding("surface_capture", "the gate failed", (g,), passed=False),
                AxisFinding("wake_resolution", "fine", (g,), passed=True), plan=plan)
    assert d.accepted and d.verdict is ReviewVerdict.failed


def test_an_unpassed_axis_outside_the_required_set_does_not_flip_the_verdict():
    L = EvidenceLedger()
    gate = L.add_gate("g", EvidenceStatus.FAIL, "bad", "executor")
    d = _accept(L, *_clean_findings(L, *AXES),
                AxisFinding("not_a_required_axis", "bad", (gate,), passed=False))
    assert not d.accepted, "a finding for an unknown axis is itself a rejection"
    assert d.verdict is None


def test_the_decision_verdict_is_always_none_or_one_of_exactly_two():
    L = EvidenceLedger()
    for findings in ((), _clean_findings(L, *AXES)):
        d = _accept(L, *findings)
        assert d.verdict in (None, ReviewVerdict.passed, ReviewVerdict.failed)


def test_an_omitted_judgement_is_malformed_input_not_a_passing_axis():
    from meshpipeline.agents.reviewer.unified import parse_findings
    findings, problems = parse_findings({"axis_findings": [
        {"axis_key": "surface_capture", "finding": "ok", "evidence_ids": ["e1"]}]})
    assert findings == (), "no AxisFinding may be constructed from an incomplete payload"
    assert any("missing required field(s) passed" in p for p in problems)


def test_the_submission_carries_the_judgement_and_no_verdict_field():
    from meshpipeline.agents.reviewer.unified import UNIFIED_TOOLS
    sf = next(t for t in UNIFIED_TOOLS if t["function"]["name"] == "submit_findings")
    props = sf["function"]["parameters"]["properties"]
    assert "verdict" not in props, "the model must never declare a verdict"
    item = props["axis_findings"]["items"]
    assert item["properties"]["passed"]["type"] == "boolean"
    assert "satisfied" not in item["properties"] and "satisfied" not in item["required"]


def test_the_decision_cannot_represent_an_accepted_review_without_a_verdict():
    from meshpipeline.agents.reviewer.eligibility import Eligibility, EligibilityDecision
    with pytest.raises(ValueError):
        EligibilityDecision(outcome=Eligibility.PASS, verdict=None)
    with pytest.raises(ValueError):
        EligibilityDecision(outcome=Eligibility.FAIL, verdict=None)


def test_the_decision_cannot_represent_a_rejected_review_with_a_verdict():
    from meshpipeline.agents.reviewer.eligibility import Eligibility, EligibilityDecision
    for bad in (ReviewVerdict.passed, ReviewVerdict.failed):
        with pytest.raises(ValueError):
            EligibilityDecision(outcome=Eligibility.REJECT, verdict=bad)


def test_an_outcome_cannot_carry_the_opposite_verdict():
    from meshpipeline.agents.reviewer.eligibility import Eligibility, EligibilityDecision
    with pytest.raises(ValueError):
        EligibilityDecision(outcome=Eligibility.PASS, verdict=ReviewVerdict.failed)


# budget-aware, deterministic corrections
def _policy(*axes, max_rounds=30):
    from meshpipeline.agents.reviewer.loop_policy import ReviewLoopPolicy
    from meshpipeline.contracts.agent_loop import LoopLimits
    return ReviewLoopPolicy(plan=_plan(*axes), ledger=EvidenceLedger(), runtime=None,
                            limits_=LoopLimits(max_rounds=max_rounds, no_progress_threshold=3))


def _reject(policy, findings=()):
    import asyncio

    from meshpipeline.agents.loop.accounting import ToolInvocation
    args = {"axis_findings": [{"axis_key": f.axis_key, "finding": f.finding,
                               "evidence_ids": list(f.evidence_ids),
                               "passed": f.passed} for f in findings],
            "rebuild_required": False, "reasoning": "r"}
    inv = ToolInvocation(round_index=1, call_index=1, tool="submit_findings",
                         raw_arguments="{}", parsed=args)
    return asyncio.run(policy.execute(inv)).content


def test_a_rejection_names_the_exact_unresolved_axes():
    text = _reject(_policy())
    assert "Submission rejected." in text
    assert "Missing axes: surface_capture, wake_resolution" in text


def test_a_rejection_says_whether_new_evidence_is_required():
    assert "New usable evidence is REQUIRED" in _reject(_policy())


def test_a_rejection_names_duplicate_and_ungrounded_axes_separately():
    p = _policy()
    bad = p.ledger.add_validation("no image", "zoom")
    text = _reject(p, (AxisFinding("surface_capture", "a", (bad,)),
                       AxisFinding("surface_capture", "b", (bad,)),
                       AxisFinding("wake_resolution", "c", (bad,))))
    assert "Duplicate axes: surface_capture" in text
    assert "Ungrounded axes:" in text


def test_a_rejection_names_unknown_evidence_ids():
    text = _reject(_policy(), (AxisFinding("surface_capture", "x", ("t-999",)),))
    assert "Unknown evidence ids: t-999" in text


def test_an_unchanged_resubmission_is_told_so_explicitly():
    p = _policy()
    _reject(p)
    text = _reject(p)                     # identical claim
    assert "materially unchanged" in text
    assert "Do not resubmit unchanged findings" in text


def test_a_changed_resubmission_is_not_accused_of_repeating():
    p = _policy()
    e = p.ledger.add_render_view("iso", "k", "open", image_ok=True)
    _reject(p)
    text = _reject(p, (AxisFinding("surface_capture", "now grounded", (e,)),))
    assert "materially unchanged" not in text


def test_the_escalated_correction_states_the_remaining_budget_and_the_consequence():
    from meshpipeline.contracts.agent_loop import LoopStage, LoopTally, ProgressObservation
    p = _policy()
    _reject(p)
    _reject(p)
    text = p.correction(LoopStage.closing, LoopTally(rounds=17),
                        ProgressObservation(made_progress=False, signature="s"))
    assert "Rounds used: 17. Rounds remaining: 13." in text
    assert "these deficits have not changed" in text.lower()
    assert "ends without a verdict" in text


def test_a_correction_carries_no_prompt_or_reasoning():
    p = _policy()
    text = _reject(p)
    for banned in ("You are a mesh-quality reviewer", "system", "chain of thought"):
        assert banned not in text


def test_a_progressing_round_gets_no_correction():
    from meshpipeline.contracts.agent_loop import LoopStage, LoopTally, ProgressObservation
    p = _policy()
    assert p.correction(LoopStage.running, LoopTally(rounds=1),
                        ProgressObservation(made_progress=True, signature="s")) is None


def test_a_malformed_submission_is_corrected_not_silently_dropped():
    import asyncio

    from meshpipeline.agents.loop.accounting import ToolInvocation
    p = _policy()
    inv = ToolInvocation(round_index=1, call_index=1, tool="submit_findings",
                         raw_arguments="{not json", parsed=None)
    out = asyncio.run(p.execute(inv))
    assert out.accepted is False and "could not be parsed" in out.content
    assert p.submissions == 0, "a malformed payload is not a submission"


def test_the_plaintext_nudge_states_the_remaining_budget():
    from meshpipeline.contracts.agent_loop import LoopTally
    assert "Rounds remaining: 25" in _policy().on_plaintext(LoopTally(rounds=5)).message


def test_a_prior_attempt_verdict_cannot_influence_the_current_one():
    from meshpipeline.agents.reviewer.loop_policy import ReviewLoopPolicy
    from meshpipeline.contracts.agent_loop import LoopLimits
    for _ in range(2):
        p = ReviewLoopPolicy(plan=_plan(), ledger=EvidenceLedger(), runtime=None,
                             limits_=LoopLimits(max_rounds=30))
        assert p.accepted is None and p.accepted_findings == ()
        assert p.submissions == 0 and p.rejections == 0
        assert p.extension().accepted_verdict is None


@pytest.mark.parametrize("prior", ["PASS", "FAIL"])
def test_a_prior_attempt_verdict_is_not_a_terminal_verdict(prior):
    from meshpipeline.application.final_result import (
        ReviewExecution,
        TerminalStatus,
        build_final_result,
    )
    fr = build_final_result(
        job_id="j", owner_id="o", status=TerminalStatus.failed, engine="snappy",
        purpose="external_cfd", dimensionality="3D", approved_snapshot_id="",
        executor_success=True, reviewer_verdict=prior, failed_gate="",
        api_failure="reviewer_evidence_missing", attempts=2, attempts_max=5,
        required_ready=False, delivered_types=[], optional_warnings=[])
    assert fr.reviewer_verdict is None
    assert fr.review_execution is ReviewExecution.failed_to_complete


@pytest.fixture(autouse=True)
def _reviewer_execution_publisher(monkeypatch):
    # the reviewer publishes through the ownership-checked port; this suite is about the review
    from tests.execution_publisher_double import install

    import meshpipeline.agents.reviewer.visual as _visual
    import meshpipeline.application.execution_publisher as _ep
    made = install(monkeypatch, _ep)
    monkeypatch.setattr(_visual, "execution_publisher", _ep.execution_publisher)
    return made
