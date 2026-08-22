# Responsibility: Verify cfMesh's weak review axes each demand their own target evidence, waivable by nothing.
from __future__ import annotations

import pytest

from meshpipeline.agents.reviewer.deterministic_evidence import collect_deterministic_evidence
from meshpipeline.agents.reviewer.eligibility import (
    AxisFinding,
    Eligibility,
    evaluate_eligibility,
)
from meshpipeline.contracts.evidence_ledger import (
    EvidenceLedger,
    InspectionTargetRef,
    TargetObligation,
)
from meshpipeline.contracts.review_evidence import (
    RenderTargetRequirement,
    RenderViewRequirement,
    TargetKind,
)
from meshpipeline.engines.assurance import derive_assurance_plan
from meshpipeline.engines.registry import get_spec

_SPEC = get_spec("cfmesh")
_PLAN = derive_assurance_plan(_SPEC, "")
_AXES = list(_PLAN.axis_names)
_WEAK = ("surface_staircasing_adequacy", "local_refinement_presence", "near_wall_layers_generated")


def _seed(*, patch=True, region=False, metric_ok=True):
    L = EvidenceLedger()
    collect_deterministic_evidence(
        _PLAN, L, measurements={"max_non_ortho": 10.0 if metric_ok else 1e6},
        criteria={c.key: c for c in _SPEC.criteria},
        # node_reviewer supplies each blocking gate as an explicit pass from the verified
        # executor_success; mirror that here so the ledger has real gate evidence.
        gate_status=dict.fromkeys(_PLAN.required_gate_keys, "pass"))
    ids = {"view": L.add_render_view("iso", "mesh_paths.surface", "open", True)}
    ids["metric"] = next(m.evidence_id for m in L.metrics() if m.metric_key == "max_non_ortho")
    if patch:
        ids["patch"] = L.add_target_inspection(
            InspectionTargetRef(TargetKind.PATCH, "patch:wall"), "toggle_patch", True, True)
    if region:
        ids["region"] = L.add_target_inspection(
            InspectionTargetRef(TargetKind.REGION, "region:core"), "inspect_region", True, True)
    return L, ids


def _all_findings(cite):
    return tuple(AxisFinding(a, f"{a} ok", tuple(cite)) for a in _AXES)


# the requirements exist and are intentional
def test_the_three_weak_axes_now_declare_typed_requirements():
    by_name = {ax.name: ax for ax in _PLAN.axes}
    for name in _WEAK:
        reqs = tuple(getattr(by_name[name], "requires", ()))
        assert reqs, f"{name} still has no typed requirements"
        assert any(isinstance(r, RenderTargetRequirement) for r in reqs), (
            f"{name} must require target-specific evidence, not just a view")


def test_surface_staircasing_requires_a_view_and_a_patch():
    ax = {a.name: a for a in _PLAN.axes}["surface_staircasing_adequacy"]
    kinds = {type(r) for r in ax.requires}
    assert RenderViewRequirement in kinds and RenderTargetRequirement in kinds


# the opening image alone cannot satisfy them
def test_opening_image_alone_cannot_satisfy_a_local_axis():
    # a full ledger (so global obligations are met), but the weak-axis findings cite ONLY the
    # opening view -> per-finding suitability fails (they require a PATCH inspection)
    L, ids = _seed(patch=True)
    findings = []
    for a in _AXES:
        cite = [ids["view"]] if a in _WEAK else [ids["view"], ids["patch"], ids["metric"]]
        findings.append(AxisFinding(a, f"{a} ok", tuple(cite)))
    d = evaluate_eligibility(_PLAN, L, tuple(findings),
                      obligations=(TargetObligation(TargetKind.PATCH),))
    assert d.outcome is Eligibility.REJECT
    assert any("an inspection of any patch" in r for r in d.reasons), d.reasons


@pytest.mark.parametrize("axis", _WEAK)
def test_each_weak_axis_isolated_needs_its_own_target_evidence(axis):
    L, ids = _seed(patch=True, region=True)
    findings = []
    for a in _AXES:
        cite = ([ids["view"], ids["metric"]] if a == axis
                else [ids["view"], ids["patch"], ids["region"], ids["metric"]])
        findings.append(AxisFinding(a, f"{a} ok", tuple(cite)))
    obs = (TargetObligation(TargetKind.PATCH),
           TargetObligation(TargetKind.REGION, exact_ids=("region:core",)))
    d = evaluate_eligibility(_PLAN, L, tuple(findings), obligations=obs)
    assert d.outcome is Eligibility.REJECT, f"{axis} was satisfied by the opening image alone"
    assert any(axis in r for r in d.reasons)


def test_a_region_inspection_cannot_satisfy_a_patch_requirement():
    # near_wall_layers requires a PATCH inspection; a REGION inspection is the wrong kind
    L, ids = _seed(patch=False, region=True)
    findings = _all_findings([ids["view"], ids["region"], ids["metric"]])
    d = evaluate_eligibility(_PLAN, L, findings, obligations=(TargetObligation(TargetKind.PATCH),))
    assert d.outcome is Eligibility.REJECT
    # the PATCH-requiring axes are unsatisfied by a region-only citation
    assert any("an inspection of any patch" in r for r in d.reasons), d.reasons


# target-specific evidence satisfies them
def test_a_patch_inspection_satisfies_the_strengthened_axes():
    L, ids = _seed(patch=True)
    findings = _all_findings([ids["view"], ids["patch"], ids["metric"]])
    d = evaluate_eligibility(_PLAN, L, findings, obligations=(TargetObligation(TargetKind.PATCH),))
    assert d.outcome is Eligibility.PASS, d.reasons


# applicability: region-less vs authored regions
def test_region_less_cfmesh_gains_no_false_region_obligation():
    # obligations = PATCH only (engine resolver produces no REGION for a region-less job)
    obs = _SPEC.expected_target_obligations({"patches": {"wall": 1}}, {})
    assert {o.kind for o in obs} == {TargetKind.PATCH}
    L, ids = _seed(patch=True)              # no region inspected, none authored
    findings = _all_findings([ids["view"], ids["patch"], ids["metric"]])
    d = evaluate_eligibility(_PLAN, L, findings, obligations=obs)
    assert d.outcome is Eligibility.PASS, d.reasons


def test_authored_region_becomes_an_exact_obligation_that_cannot_be_waived():
    obs = _SPEC.expected_target_obligations(
        {"inspection_regions": [{"name": "wake"}]}, {})
    region_obs = [o for o in obs if o.kind is TargetKind.REGION]
    assert region_obs and region_obs[0].exact_ids == ("region:wake",)
    # authored 'wake' region NOT inspected -> missing, blocks PASS
    L, ids = _seed(patch=True)             # only a patch inspected, no region:wake
    findings = _all_findings([ids["view"], ids["patch"], ids["metric"]])
    d = evaluate_eligibility(_PLAN, L, findings, obligations=obs)
    assert d.outcome is Eligibility.REJECT
    assert any("region:wake" in r for r in d.reasons)


def test_failed_metric_and_missing_evidence_still_cannot_pass():
    # failed max_non_ortho -> blocked
    L, ids = _seed(patch=True, metric_ok=False)
    d = evaluate_eligibility(_PLAN, L, _all_findings([ids["view"], ids["patch"], ids["metric"]]),
                      obligations=(TargetObligation(TargetKind.PATCH),))
    assert d.outcome is Eligibility.REJECT
    # no patch inspected at all -> blocked
    L2, ids2 = _seed(patch=False)
    d2 = evaluate_eligibility(_PLAN, L2, _all_findings([ids2["view"], ids2["metric"]]),
                       obligations=(TargetObligation(TargetKind.PATCH),))
    assert d2.outcome is Eligibility.REJECT
