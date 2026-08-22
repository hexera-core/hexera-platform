# Responsibility: Verify the evidence ledger's typing, usability and conflict rules, and what grounds each verdict.
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from meshpipeline.agents.reviewer.deterministic_evidence import collect_deterministic_evidence
from meshpipeline.agents.reviewer.eligibility import (
    AxisFinding,
    Eligibility,
    deterministic_evidence_complete,
    evaluate_eligibility,
)
from meshpipeline.agents.reviewer.unified import (
    UNIFIED_TOOLS,
    record_operation_evidence,
    run_unified_review,
)
from meshpipeline.contracts.evidence_ledger import (
    EvidenceLedger,
    EvidenceStatus,
    InspectionTargetRef,
)
from meshpipeline.contracts.review_evidence import (
    EvidenceItem,
    InspectionTarget,
    MetricRequirement,
    RenderTargetRequirement,
    RenderViewRequirement,
    TargetKind,
)


# fakes
class _Ax:
    def __init__(self, name, requires=()):
        self.name = name
        self.requires = requires


class _Plan:

    def __init__(self, *, axes=(), gates=frozenset(), metrics=frozenset(), targets=(),
                 requires_render=True):
        self.engine = "test"
        self.purpose = "p"
        self.axes = axes
        self.required_gate_keys = frozenset(gates)
        self.required_metric_keys = frozenset(metrics)
        self.required_render_artifacts = frozenset()
        self.required_targets = targets
        self.requires_render = requires_render


class _Crit:

    def __init__(self, key, ok, label=""):
        self.key = key
        self.label = label or key
        self._ok = ok            # True | False | None

    def evaluate(self, measurements):
        return self._ok


def _vmtk_like_plan():
    return _Plan(
        gates={"rc"}, metrics={"layer_coverage"},
        axes=(
            _Ax("opening_integrity", (RenderViewRequirement("iso"),
                                      RenderTargetRequirement(TargetKind.OPENING, "opening:*"))),
            _Ax("local_anatomical_fidelity", (MetricRequirement("layer_coverage"),
                                              RenderTargetRequirement(TargetKind.LAYER_REGION,
                                                                      "layer_region:*"))),
        ))


def _seed_full_ledger():
    L = EvidenceLedger()
    g = L.add_gate("rc", EvidenceStatus.PASS, "clean", "executor")
    m = L.add_metric("layer_coverage", 0.82, True, EvidenceStatus.PASS, "82%", "criteria")
    v = L.add_render_view("iso", "mesh_paths.surface", "open", True)
    t_open = L.add_target_inspection(
        InspectionTargetRef(TargetKind.OPENING, "opening:inlet"), "toggle_patch", True, True)
    t_layer = L.add_target_inspection(
        InspectionTargetRef(TargetKind.LAYER_REGION, "layer_region:wall"), "toggle_patch", True,
        True)
    return L, {"g": g, "m": m, "v": v, "t_open": t_open, "t_layer": t_layer}


def _good_findings(ids):
    return (
        AxisFinding("opening_integrity", "caps clean", (ids["v"], ids["t_open"])),
        AxisFinding("local_anatomical_fidelity", "layers hold", (ids["m"], ids["t_layer"])),
    )


# ledger mechanics
def test_ids_are_unique_and_typed_by_category():
    L = EvidenceLedger()
    a = L.add_gate("rc", EvidenceStatus.PASS, "x", "s")
    b = L.add_metric("q", 1, True, EvidenceStatus.PASS, "x", "s")
    assert a != b and a.startswith("g-") and b.startswith("m-")
    assert L.get(a) is not None and a in L


def test_an_errored_gate_is_recorded_but_not_usable():
    L = EvidenceLedger()
    eid = L.add_gate("rc", EvidenceStatus.ERROR, "blew up", "executor")
    assert L.get(eid).usable is False and L.usable_gate("rc") is None


def test_target_inspection_is_usable_only_when_covered_and_imaged():
    L = EvidenceLedger()
    ok = L.add_target_inspection(InspectionTargetRef(TargetKind.OPENING, "opening:a"), "t", True, True)
    generic = L.add_target_inspection(InspectionTargetRef(TargetKind.OPENING, "opening:b"), "t", True, False)
    blank = L.add_target_inspection(InspectionTargetRef(TargetKind.OPENING, "opening:c"), "t", False, True)
    assert L.get(ok).usable and not L.get(generic).usable and not L.get(blank).usable
    covered = {r.target_id for r in L.covered_targets()}
    assert covered == {"opening:a"}


def test_validation_records_are_never_usable():
    L = EvidenceLedger()
    eid = L.add_validation("unknown target 'ghost'", "toggle_patch")
    assert L.get(eid).usable is False


def test_duplicate_id_is_rejected():
    L = EvidenceLedger()
    L.add_gate("rc", EvidenceStatus.PASS, "x", "s")
    with pytest.raises(ValueError):
        L._append(L.get("g-001"))          # re-appending the same id


def test_conflicts_block_until_resolved():
    L = EvidenceLedger()
    cid = L.add_conflict("metric passes but branch disconnected", "m-001", "t-001")
    assert L.unresolved_conflicts()
    L.resolve_conflict(cid, "t-002")
    assert not L.unresolved_conflicts()


# deterministic collector
def test_collector_records_an_absent_gate_as_unavailable_not_pass():
    plan = _vmtk_like_plan()
    # absent → UNAVAILABLE, never a usable pass
    L = EvidenceLedger()
    collect_deterministic_evidence(
        plan, L, measurements={"layer_coverage": 0.8},
        criteria={"layer_coverage": _Crit("layer_coverage", True)}, gate_status={})
    assert L.usable_gate("rc") is None
    g = [g for g in L.gates() if g.gate_key == "rc"][0]
    assert g.status is EvidenceStatus.UNAVAILABLE and not g.usable
    # explicit pass (what node_reviewer supplies) → a real, usable pass
    L2 = EvidenceLedger()
    collect_deterministic_evidence(
        plan, L2, measurements={"layer_coverage": 0.8},
        criteria={"layer_coverage": _Crit("layer_coverage", True)}, gate_status={"rc": "pass"})
    assert L2.usable_gate("rc").passed
    assert L2.usable_metric("layer_coverage").passed


def test_collector_honours_a_failed_gate_and_unacceptable_metric():
    plan = _vmtk_like_plan()
    L = EvidenceLedger()
    collect_deterministic_evidence(
        plan, L, measurements={"layer_coverage": 0.0},
        criteria={"layer_coverage": _Crit("layer_coverage", False)}, gate_status={"rc": "fail"})
    assert L.usable_gate("rc").status is EvidenceStatus.FAIL
    assert L.usable_metric("layer_coverage").status is EvidenceStatus.FAIL


def test_collector_marks_a_missing_metric_unavailable_not_pass():
    plan = _vmtk_like_plan()
    L = EvidenceLedger()
    collect_deterministic_evidence(
        plan, L, measurements={}, criteria={"layer_coverage": _Crit("layer_coverage", None)},
        gate_status={})
    m = [m for m in L.metrics() if m.metric_key == "layer_coverage"][0]
    assert m.status is EvidenceStatus.UNAVAILABLE and not m.usable
    ok, missing = deterministic_evidence_complete(plan, L)
    assert not ok and "metric:layer_coverage" in missing


# PASS eligibility (Part 10)
def test_pass_when_every_obligation_is_met():
    plan = _vmtk_like_plan()
    L, ids = _seed_full_ledger()
    d = evaluate_eligibility(plan, L, _good_findings(ids))
    assert d.outcome is Eligibility.PASS


@pytest.mark.parametrize("mutate,needle", [
    ("fail_gate", "hard gate 'rc' failed"),
    ("fail_metric", "not acceptable"),
    ("drop_view", "required view 'iso'"),
    ("drop_target", "layer_region inspection"),
])
def test_pass_is_blocked_by_each_missing_or_failed_obligation(mutate, needle):
    plan = _vmtk_like_plan()
    L = EvidenceLedger()
    g = L.add_gate("rc", EvidenceStatus.FAIL if mutate == "fail_gate" else EvidenceStatus.PASS,
                   "x", "executor")
    m = L.add_metric("layer_coverage", 0.0 if mutate == "fail_metric" else 0.8,
                     False if mutate == "fail_metric" else True,
                     EvidenceStatus.FAIL if mutate == "fail_metric" else EvidenceStatus.PASS,
                     "x", "criteria")
    v = None if mutate == "drop_view" else L.add_render_view("iso", "k", "open", True)
    t_open = L.add_target_inspection(
        InspectionTargetRef(TargetKind.OPENING, "opening:inlet"), "t", True, True)
    t_layer = None if mutate == "drop_target" else L.add_target_inspection(
        InspectionTargetRef(TargetKind.LAYER_REGION, "layer_region:wall"), "t", True, True)
    findings = (
        AxisFinding("opening_integrity", "caps", tuple(x for x in (v, t_open) if x)),
        AxisFinding("local_anatomical_fidelity", "layers", tuple(x for x in (m, t_layer) if x)),
    )
    d = evaluate_eligibility(plan, L, findings)
    assert d.outcome is Eligibility.REJECT
    assert any(needle in r for r in d.reasons), d.reasons


def test_pass_requires_every_axis_exactly_once_and_nonblank():
    plan = _vmtk_like_plan()
    L, ids = _seed_full_ledger()
    # missing the second axis
    d1 = evaluate_eligibility(plan, L, _good_findings(ids)[:1])
    assert d1.outcome is Eligibility.REJECT and any("local_anatomical_fidelity" in r for r in d1.reasons)
    # blank finding
    blank = (_good_findings(ids)[0], AxisFinding("local_anatomical_fidelity", "  ", (ids["m"],)))
    d2 = evaluate_eligibility(plan, L, blank)
    assert d2.outcome is Eligibility.REJECT and any("blank" in r for r in d2.reasons)


def test_pass_rejects_unknown_and_wrong_kind_evidence_in_a_finding():
    plan = _vmtk_like_plan()
    L, ids = _seed_full_ledger()
    # unknown evidence id
    bad1 = (AxisFinding("opening_integrity", "caps", ("t-999",)),
            AxisFinding("local_anatomical_fidelity", "layers", (ids["m"], ids["t_layer"])))
    d1 = evaluate_eligibility(plan, L, bad1)
    assert d1.outcome is Eligibility.REJECT and any("unknown evidence" in r for r in d1.reasons)
    # opening axis cited with the LAYER target (wrong exact target kind for its RenderTarget req)
    bad2 = (AxisFinding("opening_integrity", "caps", (ids["v"], ids["t_layer"])),
            AxisFinding("local_anatomical_fidelity", "layers", (ids["m"], ids["t_layer"])))
    d2 = evaluate_eligibility(plan, L, bad2)
    assert d2.outcome is Eligibility.REJECT
    # The reason must name the evidence that would satisfy it, not the rule class: a refusal the
    # reviewer cannot act on is retried until the round budget is gone. Assert the phrase this
    # branch alone produces - the bare kind name is not enough, because "opening" also occurs in
    # the axis key, so it would pass on a blank finding or on a different failing requirement.
    assert any("an inspection of any opening" in r for r in d2.reasons), d2.reasons


# A rejection is only a gate if the reviewer can act on it. These two pin the content of the
# reason, because the reason IS the repair instruction: a real review looped on "does not satisfy
# required MetricRequirement" - re-running inspections it had already run, then restating the
# metric to more decimal places - until the round budget was spent and the job failed. The metric
# it needed was on file the whole time, under an id it was never told to cite.
def test_an_unsatisfied_metric_names_the_metric_and_the_id_that_would_satisfy_it():
    plan = _vmtk_like_plan()
    L, ids = _seed_full_ledger()
    # the layers axis cites its render target but NOT the measurement its axis requires
    findings = (_good_findings(ids)[0],
                AxisFinding("local_anatomical_fidelity", "layers hold", (ids["t_layer"],)))
    d = evaluate_eligibility(plan, L, findings)
    assert d.outcome is Eligibility.REJECT
    reason = " ".join(d.reasons)
    assert "layer_coverage" in reason, f"the metric must be named: {reason}"
    assert ids["m"] in reason, f"the id that would satisfy it must be named: {reason}"
    assert "MetricRequirement" not in reason, f"a rule class is not an instruction: {reason}"


def test_an_unsatisfied_inspection_points_at_the_one_on_file_instead_of_ordering_another():
    # The commonest failure here is an axis citing the WRONG evidence, not none: the inspection it
    # needed was already taken. Telling it to go produce one sends it to re-run a tool it has
    # already run, which is the retry loop this message exists to break rather than a repair.
    plan = _vmtk_like_plan()
    L, ids = _seed_full_ledger()
    findings = (AxisFinding("opening_integrity", "caps", (ids["v"], ids["t_layer"])),
                AxisFinding("local_anatomical_fidelity", "layers", (ids["m"], ids["t_layer"])))
    d = evaluate_eligibility(plan, L, findings)
    assert d.outcome is Eligibility.REJECT
    reason = " ".join(d.reasons)
    assert ids["t_open"] in reason, f"the inspection already on file must be named: {reason}"
    assert "produce" not in reason, (
        f"it must not order a re-render of evidence the ledger already holds: {reason}")


def test_an_unsatisfied_metric_says_a_render_cannot_substitute_for_a_measurement():
    plan = _vmtk_like_plan()
    L, ids = _seed_full_ledger()
    # citing MORE renders is the wrong repair, and is exactly what the stuck review kept trying
    findings = (_good_findings(ids)[0],
                AxisFinding("local_anatomical_fidelity", "layers hold",
                            (ids["t_layer"], ids["v"])))
    d = evaluate_eligibility(plan, L, findings)
    assert d.outcome is Eligibility.REJECT
    reason = " ".join(d.reasons).lower()
    assert "measurement" in reason, f"must say what kind of evidence is wanted: {reason}"


# The veto that remains after advisory metrics stopped blocking. No engine ships a GATING
# axis-required metric today - every one of them is advisory, and genuinely gating criteria are
# refused earlier by the quality_floor flow gate - so without this test the guard has no coverage at
# all and could be deleted silently.
def test_an_all_pass_submission_may_not_contradict_a_failing_GATING_metric():
    plan = _vmtk_like_plan()
    L, ids = _seed_full_ledger()
    # replace the passing measurement with one the engine declared GATING and that failed
    L2 = EvidenceLedger()
    g = L2.add_gate("rc", EvidenceStatus.PASS, "clean", "executor")
    m = L2.add_metric("layer_coverage", 0.10, False, EvidenceStatus.FAIL, "10%", "criteria",
                      gating=True)
    v = L2.add_render_view("iso", "mesh_paths.surface", "open", True)
    t_open = L2.add_target_inspection(
        InspectionTargetRef(TargetKind.OPENING, "opening:inlet"), "toggle_patch", True, True)
    t_layer = L2.add_target_inspection(
        InspectionTargetRef(TargetKind.LAYER_REGION, "layer_region:wall"), "toggle_patch", True,
        True)
    findings = (AxisFinding("opening_integrity", "caps clean", (v, t_open)),
                AxisFinding("local_anatomical_fidelity", "layers hold", (m, t_layer)))
    d = evaluate_eligibility(plan, L2, findings)
    assert d.outcome is Eligibility.REJECT, d
    assert any("layer_coverage" in r for r in d.reasons), d.reasons
    assert g and ids  # fixtures referenced so the seeding above reads as deliberate


def test_a_failing_ADVISORY_metric_leaves_an_all_pass_submission_admissible():
    # the counterpart: same shape, gating=False, and the mesh survives to a verdict
    plan = _vmtk_like_plan()
    L2 = EvidenceLedger()
    L2.add_gate("rc", EvidenceStatus.PASS, "clean", "executor")
    m = L2.add_metric("layer_coverage", 0.10, False, EvidenceStatus.FAIL, "10%", "criteria",
                      gating=False)
    v = L2.add_render_view("iso", "mesh_paths.surface", "open", True)
    t_open = L2.add_target_inspection(
        InspectionTargetRef(TargetKind.OPENING, "opening:inlet"), "toggle_patch", True, True)
    t_layer = L2.add_target_inspection(
        InspectionTargetRef(TargetKind.LAYER_REGION, "layer_region:wall"), "toggle_patch", True,
        True)
    findings = (AxisFinding("opening_integrity", "caps clean", (v, t_open)),
                AxisFinding("local_anatomical_fidelity", "layers hold", (m, t_layer)))
    d = evaluate_eligibility(plan, L2, findings)
    assert d.outcome is Eligibility.PASS, d.reasons


def test_pass_rejects_a_duplicate_axis_finding():
    plan = _Plan(gates=set(), metrics=set(), axes=(_Ax("a", ()),))
    L = EvidenceLedger()
    v = L.add_render_view("iso", "k", "open", True)
    d = evaluate_eligibility(
        plan, L, (AxisFinding("a", "x", (v,)), AxisFinding("a", "y", (v,))))
    assert d.outcome is Eligibility.REJECT
    assert any("findings" in r and "exactly one" in r for r in d.reasons)


def test_pass_rejects_a_finding_that_cites_no_evidence():
    plan = _Plan(gates=set(), metrics=set(), axes=(_Ax("a", ()),))
    L = EvidenceLedger()
    L.add_render_view("iso", "k", "open", True)      # some usable evidence exists in the ledger
    d = evaluate_eligibility(plan, L, (AxisFinding("a", "looks fine", ()),))
    assert d.outcome is Eligibility.REJECT and any("no usable evidence" in r for r in d.reasons)


def test_generic_view_cannot_satisfy_a_target_requirement():
    plan = _Plan(axes=(_Ax("connectivity", (RenderTargetRequirement(TargetKind.BRANCH, "branch:*"),)),))
    L = EvidenceLedger()
    v = L.add_render_view("iso", "k", "open", True)
    d = evaluate_eligibility(plan, L, (AxisFinding("connectivity", "branches ok", (v,)),))
    assert d.outcome is Eligibility.REJECT


def test_unresolved_conflict_blocks_pass():
    plan = _vmtk_like_plan()
    L, ids = _seed_full_ledger()
    L.add_conflict("metric passes but opening closed", ids["m"], ids["t_open"])
    d = evaluate_eligibility(plan, L, _good_findings(ids))
    assert d.outcome is Eligibility.REJECT and any("conflict" in r for r in d.reasons)


def test_lifecycle_failure_blocks_pass():
    plan = _vmtk_like_plan()
    L, ids = _seed_full_ledger()
    from meshpipeline.agents.reviewer.eligibility import evaluate_eligibility
    d = evaluate_eligibility(plan, L, _good_findings(ids), lifecycle_ok=False)
    assert d.outcome is Eligibility.REJECT and any("lifecycle" in r for r in d.reasons)


# FAIL eligibility (Part 11)
def test_fail_is_grounded_by_a_failed_gate():
    plan = _vmtk_like_plan()
    L = EvidenceLedger()
    g = L.add_gate("rc", EvidenceStatus.FAIL, "nonzero exit", "executor")
    mid = L.add_metric("layer_coverage", 0.9, True, EvidenceStatus.PASS, "ok", "executor")
    vid = L.add_render_view("iso", "k", "open", True)
    tl = L.add_target_inspection(
        InspectionTargetRef(TargetKind.LAYER_REGION, "layer_region:wall"), "t", True, True)
    to = L.add_target_inspection(
        InspectionTargetRef(TargetKind.OPENING, "opening:inlet"), "t", True, True)
    d = evaluate_eligibility(plan, L, (
        AxisFinding("opening_integrity", "aborted", (g, vid, to), passed=False),
        AxisFinding("local_anatomical_fidelity", "layers fine", (mid, tl), passed=True)))
    assert d.outcome is Eligibility.FAIL and d.verdict is not None


def test_fail_is_grounded_by_a_target_specific_defect():
    plan = _vmtk_like_plan()
    L = EvidenceLedger()
    L.add_gate("rc", EvidenceStatus.PASS, "ok", "executor")
    mid = L.add_metric("layer_coverage", 0.9, True, EvidenceStatus.PASS, "ok", "executor")
    vid = L.add_render_view("iso", "k", "open", True)
    tl = L.add_target_inspection(
        InspectionTargetRef(TargetKind.LAYER_REGION, "layer_region:wall"), "t", True, True)
    t = L.add_target_inspection(InspectionTargetRef(TargetKind.OPENING, "opening:inlet"), "t", True, True)
    d = evaluate_eligibility(plan, L, (
        AxisFinding("opening_integrity", "cap malformed", (vid, t), passed=False),
        AxisFinding("local_anatomical_fidelity", "layers fine", (mid, tl), passed=True)))
    assert d.outcome is Eligibility.FAIL and d.verdict is not None


def test_fail_rejected_when_only_a_generic_view_or_no_evidence():
    plan = _vmtk_like_plan()
    L = EvidenceLedger()
    v = L.add_render_view("iso", "k", "open", True)
    assert evaluate_eligibility(plan, L, (AxisFinding("opening_integrity", "looks bad", (v,)),)).outcome is Eligibility.REJECT
    assert evaluate_eligibility(plan, L, (AxisFinding("opening_integrity", "bad", ()),)).outcome is Eligibility.REJECT
    assert evaluate_eligibility(plan, L, (AxisFinding("opening_integrity", "bad", ("x-999",)),)).outcome is Eligibility.REJECT


# record_operation_evidence typing
def _ev(**kw):
    base = {"evidence_id": "s-1", "seq": 1, "label": "l", "purpose": "p", "image_ref": "/x.png"}
    base.update(kw)
    return EvidenceItem(**base)


def test_record_types_by_the_sessions_covers_target_not_the_request():
    L = EvidenceLedger()
    inv = {"opening:inlet": InspectionTarget("opening:inlet", TargetKind.OPENING, "cap", "pp", False)}
    eid = record_operation_evidence(L, _ev(covers_target="opening:inlet"), inv, "toggle_patch")
    rec = L.get(eid)
    assert rec.category == "target_inspection" and rec.target.kind is TargetKind.OPENING and rec.usable


def test_a_failed_render_records_a_validation_not_coverage():
    L = EvidenceLedger()
    eid = record_operation_evidence(L, _ev(image_ref="", covers_target="opening:inlet",
                                           diagnostics=("unknown target",)), {}, "toggle_patch")
    assert L.get(eid).category == "validation" and not L.get(eid).usable


def test_a_view_frame_records_a_render_view():
    L = EvidenceLedger()
    eid = record_operation_evidence(L, _ev(view_id="iso"), {}, "set_camera_preset")
    assert L.get(eid).category == "render_view" and L.get(eid).view_id == "iso"


# run_unified_review over fakes
class _FakeRuntime:

    def __init__(self, scripted):
        self._scripted = scripted        # fn -> EvidenceItem

    async def execute_typed(self, fn, args):
        from meshpipeline.agents.reviewer.render_runtime import NOT_VIEWER_TOOL, TypedToolResult
        if fn == "submit_findings":
            return TypedToolResult(content=NOT_VIEWER_TOOL, evidence=None, is_viewer_tool=False)
        ev = self._scripted.get(fn)
        content = [{"type": "text", "text": "ok"},
                   {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}] \
            if ev and ev.image_ref else "refused"
        return TypedToolResult(content=content, evidence=ev, is_viewer_tool=True)


class _Opening:
    def __init__(self, targets, has_geometry=True):
        self.has_geometry = has_geometry
        self.initial_screenshot_b64 = "AAAA"
        self.inspection_targets = targets


def _tc(name, **args):
    import json

    from meshpipeline.contracts.model_inference import ToolCallRequest
    return ToolCallRequest(id=f"c{name}", name=name, arguments=json.dumps(args))


def _resp(*tool_calls, content=""):
    from meshpipeline.contracts.model_inference import ModelRoundResult
    return ModelRoundResult(tool_calls=tuple(tool_calls), assistant_text=content,
                            finish_reason="tool_calls" if tool_calls else "stop")


def _scripted_provider(script):
    calls = iter(script)

    async def provider(*, messages, tools, job_id, user_id):
        try:
            return next(calls)
        except StopIteration:
            return _resp()          # no tool calls -> loop nudges / exhausts
    return provider


def _run(coro):
    return asyncio.run(coro)


def test_unified_pass_records_evidence_and_accepts():
    plan = _vmtk_like_plan()
    L = EvidenceLedger()
    collect_deterministic_evidence(
        plan, L, measurements={"layer_coverage": 0.8},
        criteria={"layer_coverage": _Crit("layer_coverage", True)},
        gate_status={"rc": "pass"})   # node_reviewer supplies the executor's verified gate pass
    targets = (
        InspectionTarget("opening:inlet", TargetKind.OPENING, "cap", "pp", False),
        InspectionTarget("layer_region:wall", TargetKind.LAYER_REGION, "wall", "pp", False),
    )
    runtime = _FakeRuntime({
        "toggle_patch": _ev(covers_target="opening:inlet", view_id=""),
    })
    # the inlet inspection lands as t-... ; the layer we inject via a second scripted op
    runtime2 = _FakeRuntime({})

    # script: inspect opening, inspect layer (reuse toggle by swapping the scripted ev per call is
    # awkward with one dict) -> instead drive one op then submit with ledger pre-seeded targets.
    L.add_render_view("iso", "mesh_paths.surface", "open", True)
    to = L.add_target_inspection(InspectionTargetRef(TargetKind.OPENING, "opening:inlet"), "toggle_patch", True, True)
    tl = L.add_target_inspection(InspectionTargetRef(TargetKind.LAYER_REGION, "layer_region:wall"), "toggle_patch", True, True)
    mid = [m.evidence_id for m in L.metrics()][0]
    vid = [v.evidence_id for v in L.render_views()][0]
    script = [
        _resp(_tc("submit_findings", rebuild_required=False, reasoning="all good", axis_findings=[
            {"axis_key": "opening_integrity", "finding": "caps clean", "evidence_ids": [vid, to], "passed": True},
            {"axis_key": "local_anatomical_fidelity", "finding": "layers hold", "evidence_ids": [mid, tl], "passed": True},
        ])),
    ]
    out = _run(run_unified_review(
        plan=plan, ledger=L, runtime=runtime2, opening=_Opening(targets),
        system_prompt="sys", opening_context_text="ctx",
        provider_call=_scripted_provider(script), max_rounds=6, job_id="j"))
    assert out.verdict == "PASS" and not out.api_failure


def test_unified_blocks_pass_on_a_failed_gate_then_exhausts_to_non_verdict():
    plan = _vmtk_like_plan()
    L = EvidenceLedger()
    collect_deterministic_evidence(
        plan, L, measurements={"layer_coverage": 0.8},
        criteria={"layer_coverage": _Crit("layer_coverage", True)}, gate_status={"rc": "fail"})
    L.add_render_view("iso", "k", "open", True)
    to = L.add_target_inspection(InspectionTargetRef(TargetKind.OPENING, "opening:inlet"), "t", True, True)
    tl = L.add_target_inspection(InspectionTargetRef(TargetKind.LAYER_REGION, "layer_region:wall"), "t", True, True)
    mid = [m.evidence_id for m in L.metrics()][0]
    vid = [v.evidence_id for v in L.render_views()][0]
    submit = _tc("submit_findings", rebuild_required=False, reasoning="x", axis_findings=[
        {"axis_key": "opening_integrity", "finding": "caps", "evidence_ids": [vid, to], "passed": True},
        {"axis_key": "local_anatomical_fidelity", "finding": "layers", "evidence_ids": [mid, tl], "passed": True},
    ])
    script = [_resp(submit)] * 12
    out = _run(run_unified_review(
        plan=plan, ledger=L, runtime=_FakeRuntime({}), opening=_Opening(()),
        system_prompt="s", opening_context_text="c",
        provider_call=_scripted_provider(script), max_rounds=12, job_id="j"))
    # PASS never becomes eligible (gate failed) -> truthful non-verdict, NOT a fabricated verdict.
    # The identical resubmission now ends the review through no-progress rather than burning the
    # whole round budget; the outcome is the same truthful non-verdict either way.
    assert out.verdict == "" and out.api_failure == "reviewer_evidence_missing"
    assert out.failure_class == "eligibility_non_convergence"
    assert out.llm_rounds < 12, "an unchanged resubmission must not consume the whole budget"


def test_unified_refuses_provider_when_deterministic_evidence_missing():
    plan = _vmtk_like_plan()
    L = EvidenceLedger()          # empty - no gate/metric evidence
    called = {"n": 0}

    async def provider(**kw):
        called["n"] += 1
        return _resp()
    out = _run(run_unified_review(
        plan=plan, ledger=L, runtime=_FakeRuntime({}), opening=_Opening(()),
        system_prompt="s", opening_context_text="c", provider_call=provider, max_rounds=4))
    assert out.api_failure == "reviewer_evidence_missing" and called["n"] == 0


def test_unified_classifies_a_provider_failure_as_provider_unavailable():
    plan = _vmtk_like_plan()
    L = EvidenceLedger()
    collect_deterministic_evidence(
        plan, L, measurements={"layer_coverage": 0.8},
        criteria={"layer_coverage": _Crit("layer_coverage", True)},
        gate_status={"rc": "pass"})   # complete evidence, so the provider is actually called

    async def provider(**kw):
        from meshpipeline.contracts.model_inference import ModelRoundResult
        return ModelRoundResult(failure_marker="rate_limit")
    out = _run(run_unified_review(
        plan=plan, ledger=L, runtime=_FakeRuntime({}), opening=_Opening(()),
        system_prompt="s", opening_context_text="c", provider_call=provider, max_rounds=4))
    assert out.api_failure == "rate_limit" and out.failure_class == "provider_unavailable"


def test_unified_records_a_viewer_op_from_the_session_result():
    plan = _Plan(gates=set(), metrics=set(),
                 axes=(_Ax("connectivity", (RenderTargetRequirement(TargetKind.BRANCH, "branch:*"),)),))
    L = EvidenceLedger()
    targets = (InspectionTarget("branch:0", TargetKind.BRANCH, "b", "pp", False),)
    runtime = _FakeRuntime({"toggle_patch": _ev(covers_target="branch:0")})
    # one viewer op then FAIL grounded by the branch inspection
    script = [
        _resp(_tc("toggle_patch", patch_name="branch:0", visible=True)),
        _resp(_tc("submit_findings", rebuild_required=False, reasoning="branch pinched",
                  axis_findings=[{"axis_key": "connectivity", "passed": False, "finding": "branch:0 sealed",
                                  "evidence_ids": ["t-001"]}])),
    ]
    out = _run(run_unified_review(
        plan=plan, ledger=L, runtime=runtime, opening=_Opening(targets),
        system_prompt="s", opening_context_text="c",
        provider_call=_scripted_provider(script), max_rounds=6))
    assert out.verdict == "FAIL"
    # the branch inspection was recorded from the session's covers_target, and is usable
    assert L.usable_inspection_of_target(InspectionTargetRef(TargetKind.BRANCH, "branch:0"))


def test_region_obligation_follows_engine_resolved_obligations():
    from meshpipeline.agents.reviewer.eligibility import evaluate_eligibility
    from meshpipeline.contracts.evidence_ledger import TargetObligation
    plan = _Plan(gates=set(), metrics=set(),
                 axes=(_Ax("cap", (RenderTargetRequirement(TargetKind.REGION, "region:*"),)),))
    L = EvidenceLedger()
    v = L.add_render_view("iso", "k", "open", True)
    f = (AxisFinding("cap", "no regions in this mesh", (v,)),)
    # only a PATCH obligation -> REGION not applicable -> its axis requirement is not enforced
    na = evaluate_eligibility(plan, L, f, obligations=(TargetObligation(TargetKind.PATCH),))
    assert not any("region" in r for r in na.reasons)
    # a REGION obligation with no usable region inspection -> MISSING (blocks)
    em = evaluate_eligibility(plan, L, f, obligations=(TargetObligation(TargetKind.REGION),))
    assert any("region" in r for r in em.reasons)


def test_partial_target_loss_is_caught_by_exact_ids_and_count():
    from meshpipeline.agents.reviewer.eligibility import evaluate_eligibility
    from meshpipeline.contracts.evidence_ledger import TargetObligation
    plan = _Plan(gates=set(), metrics=set(), axes=(_Ax("a", ()),))
    L = EvidenceLedger()
    # two regions authored, only one inspected
    L.add_target_inspection(InspectionTargetRef(TargetKind.REGION, "region:midspan"), "t", True, True)
    t = [r.evidence_id for r in L.target_inspections()][0]
    f = (AxisFinding("a", "midspan ok", (t,)),)
    exact = evaluate_eligibility(plan, L, f, obligations=(
        TargetObligation(TargetKind.REGION, exact_ids=("region:midspan", "region:nearwall")),))
    assert any("region:nearwall" in r for r in exact.reasons), "the lost region must be flagged"
    # three groups expected (count), only one inspected
    L2 = EvidenceLedger()
    L2.add_target_inspection(InspectionTargetRef(TargetKind.GROUP, "group:2:1"), "t", True, True)
    g = [r.evidence_id for r in L2.target_inspections()][0]
    cnt = evaluate_eligibility(plan, L2, (AxisFinding("a", "one group", (g,)),),
                        obligations=(TargetObligation(TargetKind.GROUP, min_count=3),))
    assert any("expected 3 group" in r for r in cnt.reasons)


def test_missing_target_obligations_precheck():
    from meshpipeline.agents.reviewer.eligibility import missing_target_obligations
    from meshpipeline.contracts.evidence_ledger import TargetObligation
    obligations = (
        TargetObligation(TargetKind.OPENING, min_count=4, provenance="4 open profiles"),
        TargetObligation(TargetKind.REGION, exact_ids=("region:a", "region:b")),
    )
    discovered = {TargetKind.OPENING: {"opening:0", "opening:1"}, TargetKind.REGION: {"region:a"}}
    problems = missing_target_obligations(obligations, discovered)
    assert any("opening" in p and "2" in p for p in problems)     # 4 expected, 2 produced
    assert any("region:b" in p for p in problems)                 # exact id absent
    # fully satisfied -> no problems
    ok = missing_target_obligations(
        (TargetObligation(TargetKind.OPENING, min_count=1),),
        {TargetKind.OPENING: {"opening:0"}})
    assert ok == []


def test_validate_plan_rejects_a_requirement_for_an_undeclared_target_kind():
    from meshpipeline.agents.reviewer.eligibility import validate_plan

    class _Spec:
        def __init__(self, kinds, artifacts):
            self.inspection_targets = tuple(
                InspectionTarget(f"{k.value}:*", k, "l", "p", False) for k in kinds)
            self.render_artifacts = tuple(
                SimpleNamespace(artifact_key=a) for a in artifacts)

    # plan axis requires a BRANCH target, but the spec declares only PATCH targets
    plan = _Plan(axes=(_Ax("x", (RenderTargetRequirement(TargetKind.BRANCH, "branch:*"),)),))
    ok, problems = validate_plan(_Spec([TargetKind.PATCH], []), plan)
    assert not ok and any("branch" in p for p in problems)
    # a well-formed plan validates
    ok2, _ = validate_plan(_Spec([TargetKind.BRANCH], []), plan)
    assert ok2


def test_unified_outcome_carries_the_audit_trail():
    plan = _Plan(gates=set(), metrics=set(), axes=(_Ax("a", ()),))
    L = EvidenceLedger()
    t = L.add_target_inspection(InspectionTargetRef(TargetKind.PATCH, "patch:inlet"), "t", True, True)
    script = [
        _resp(_tc("submit_findings", rebuild_required=False, reasoning="bad",
                  axis_findings=[{"axis_key": "a", "passed": False, "finding": "patch wrong", "evidence_ids": [t]}])),
    ]
    out = _run(run_unified_review(
        plan=plan, ledger=L, runtime=_FakeRuntime({}), opening=_Opening(()),
        system_prompt="s", opening_context_text="c",
        provider_call=_scripted_provider(script), max_rounds=6))
    assert out.verdict == "FAIL" and out.messages and out.llm_rounds >= 1


def test_the_unified_tool_set_uses_evidence_linked_findings():
    names = {t["function"]["name"] for t in UNIFIED_TOOLS}
    assert "submit_findings" in names
    sf = next(t for t in UNIFIED_TOOLS if t["function"]["name"] == "submit_findings")
    item = sf["function"]["parameters"]["properties"]["axis_findings"]["items"]["properties"]
    # `satisfied` is the per-axis judgement the application derives the verdict from
    assert set(item) == {"axis_key", "finding", "evidence_ids", "passed"}
    assert set(sf["function"]["parameters"]["properties"]["axis_findings"]["items"]
               ["required"]) == {"axis_key", "finding", "evidence_ids", "passed"}


@pytest.fixture(autouse=True)
def _reviewer_execution_publisher(monkeypatch):
    # the reviewer publishes through the ownership-checked port; this suite is about the review
    from tests.execution_publisher_double import install

    import meshpipeline.agents.reviewer.visual as _visual
    import meshpipeline.application.execution_publisher as _ep
    made = install(monkeypatch, _ep)
    monkeypatch.setattr(_visual, "execution_publisher", _ep.execution_publisher)
    return made
