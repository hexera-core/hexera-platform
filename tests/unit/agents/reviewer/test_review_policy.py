# Responsibility: Verify what each review finding may do - fail the job only as a confirmed wrong problem, rebuild only for a cited buildable change, otherwise stand as a concern.
# Boundaries: review_policy's pure judgement, over real-shaped findings, evidence and measurements.
"""Both sides of the line between "wrong problem" and "imperfect mesh".

Tonight's rejections (2026-10-01, gpt-5.6-terra) are replayed from their own submissions: every one
of them ends as a concern on a delivered mesh. Each wrong-problem class is constructed once and must
stay blocking - and the measured vetoes must turn a class the gates' numbers contradict back into a
concern.
"""
from __future__ import annotations

import pytest

from meshpipeline.agents.reviewer.eligibility import AxisFinding
from meshpipeline.agents.reviewer.review_policy import (
    LEVERS,
    WRONG_PROBLEM,
    asks_for_rebuild,
    blocking_findings,
    brief_quote_found,
    builder_levers,
    judge,
    judge_all,
)
from meshpipeline.contracts.evidence_ledger import EvidenceLedger, EvidenceStatus, InspectionTargetRef
from meshpipeline.contracts.review_evidence import TargetKind

# The CRM job's approved texts, as the reviewer is shown them (request, criteria, confirmed setup).
CRM_BRIEF = "\n".join([
    "Takeoff aero on this half-model airliner, 70 m/s. Air at 15 °C and sea-level pressure. "
    "Steady RANS with k-ω SST, wall functions with y⁺ = 30–300. 5 prism layers. Local "
    "refinement at the leading edges, trailing edges, wing–body junctions, and wake.",
    "The mesh must enclose the half-model with the symmetry plane on the cut face.",
    "CONFIRMED SETUP (approved by the user before meshing; quote it word for word when a "
    "finding contradicts it):\n  Boundaries: aircraft as wall; farfield as farfield; symmetry as "
    "symmetry.\n  Flow direction: +x.\n  Reference length: 64.593 m.\n  Outer-domain margins in "
    "reference lengths: up 5, down 10, side 5, vert 5.\n  Dimensionality: 3D.",
])
PIPE_BRIEF = ("Internal water flow through the manifold. CONFIRMED SETUP: Boundaries: inlet as "
              "inlet; outlet_1 as outlet; wall as wall.")
SNAPPY_EXTERNAL = ("domain", "refinement", "layers", "boundaries")

# what the gates measured on e0fa8ad0 attempt 2
CRM_MEASURED = {"regions": 1, "layer_coverage_pct": 48.0194, "reference_length_m": 64.593,
                "body_length_m": 64.6, "surface_deviation": {"mean_ratio": 0.0082,
                                                             "p95_ratio": 0.0334,
                                                             "frac_beyond_one_cell": 0.0001}}


@pytest.fixture
def ledger():
    L = EvidenceLedger()
    ids = {
        "slice": L.add_target_inspection(InspectionTargetRef(TargetKind.REGION, "region:slice_y"),
                                         "inspect_region", image_ok=True, covered=True),
        "patch": L.add_target_inspection(InspectionTargetRef(TargetKind.PATCH, "patch:aircraft"),
                                         "toggle_patch", image_ok=True, covered=True),
        "iso": L.add_render_view("iso", "mesh_paths.surface", "open", image_ok=True),
        "metric": L.add_metric("max_non_ortho", 65.03, False, EvidenceStatus.FAIL, "65.03",
                               "criteria", gating=False),
    }
    return L, ids


def _f(axis, ev, **kw) -> AxisFinding:
    return AxisFinding(axis_key=axis, finding=kw.pop("finding", "x"), evidence_ids=tuple(ev),
                       passed=False, **kw)


def _j(f, L, brief=CRM_BRIEF, levers=SNAPPY_EXTERNAL, measured=None):
    return judge(f, brief_text=brief, levers=levers, ledger=L,
                 measured=CRM_MEASURED if measured is None else measured)


# the citation check

def test_a_quote_must_really_be_in_the_brief():
    assert brief_quote_found("5 prism layers", CRM_BRIEF)
    assert brief_quote_found("wall functions with y+ = 30-300", CRM_BRIEF)   # y⁺ and – fold
    assert brief_quote_found("Local refinement at the leading edges ... and wake", CRM_BRIEF)
    assert brief_quote_found("SYMMETRY AS SYMMETRY", CRM_BRIEF)
    # what the reviewer read into the builder's configuration is not the brief
    assert not brief_quote_found("a dedicated downstream wake refinement region", CRM_BRIEF)
    assert not brief_quote_found("exactly 5 prism layers on every face", CRM_BRIEF)
    # too short to identify a requirement
    assert not brief_quote_found("wake", CRM_BRIEF)
    assert not brief_quote_found("", CRM_BRIEF)
    # fragments must appear in order
    assert not brief_quote_found("and wake ... Local refinement at the leading", CRM_BRIEF)


def test_levers_come_from_what_the_engine_and_workflow_declare():
    from meshpipeline.engines.registry import get_spec
    assert builder_levers(get_spec("snappy"), "external_cfd") == LEVERS
    assert "domain" not in builder_levers(get_spec("snappy"), "internal_cfd")
    assert set(builder_levers(get_spec("gmsh"), "structural")) <= set(LEVERS)
    assert "refinement" in builder_levers(get_spec("gmsh"), "structural")


# tonight's rejections, replayed from their own submissions

def test_crm_e0fa8ad0_findings_as_submitted_are_concerns(ledger):
    L, ids = ledger
    prism = _f("prism_layer_coverage", [ids["metric"], ids["slice"]],
               finding="FAIL. Aircraft prism coverage is only 59.0%, averaging 2.5 of the "
                       "requested 5 layers.")
    wake = _f("wake_resolution", [ids["slice"]],
              finding="FAIL. The supplied refinement setup is distance-based around the aircraft "
                      "only, with no distinct downstream wake refinement region.")
    out = judge_all([prism, wake], brief_text=CRM_BRIEF, levers=SNAPPY_EXTERNAL, ledger=L,
                    measured=CRM_MEASURED)
    assert all(not j.blocking and not j.improve for j in out.values())
    assert out["prism_layer_coverage"].reason == "it cites no requirement from your brief"


def test_crm_partial_layers_cannot_be_called_absent(ledger):
    L, ids = ledger
    # even a reviewer that labels 48% coverage "requirement absent" is overruled by the measurement
    j = _j(_f("prism_layer_coverage", [ids["slice"]], brief_requirement="5 prism layers",
              wrong_problem="requirement_absent"), L)
    assert not j.blocking and "present, not absent" in j.reason
    assert j.severity == "material"          # shown prominently, still delivered


def test_crm_wake_read_from_the_config_is_a_concern(ledger):
    L, ids = ledger
    j = _j(_f("wake_resolution", [ids["slice"]], wrong_problem="requirement_absent",
              brief_requirement="no distinct downstream wake refinement region"), L)
    assert not j.blocking and j.reason == "the fact it quotes is not in your brief or the confirmed setup"


def test_c608_76ad00ec_findings_as_submitted_are_concerns(ledger):
    L, ids = ledger
    measured = {**CRM_MEASURED, "layer_coverage_pct": 68.1181}
    out = judge_all([_f("prism_layer_coverage", [ids["slice"]],
                        finding="Fail. 74.5% layer coverage and an average 3.41 of 5 layers."),
                     _f("wake_resolution", [ids["slice"]],
                        finding="Fail. No deliberately extended downstream wake zone.")],
                    brief_text=CRM_BRIEF, levers=SNAPPY_EXTERNAL, ledger=L, measured=measured)
    assert all(not j.blocking and not j.improve for j in out.values())


def test_sae_8e8e8309_shape_call_is_overruled_by_the_measured_snap(ledger):
    L, ids = ledger
    sae = "Complete SAE notchback car at 40 m/s with focused near-wake refinement."
    measured = {"regions": 1, "surface_deviation": {"p95_ratio": 0.0002,
                                                    "frac_beyond_one_cell": 0.0}}
    as_submitted = _j(_f("surface_capture", [ids["patch"], ids["iso"]],
                         finding="The rendered car is a wedge, not a notchback."), L,
                      brief=sae, measured=measured)
    assert not as_submitted.blocking
    labelled = _j(_f("surface_capture", [ids["patch"]], wrong_problem="shape_changed",
                     brief_requirement="Complete SAE notchback car"), L, brief=sae,
                  measured=measured)
    assert not labelled.blocking and "within one cell of the CAD" in labelled.reason


# genuinely wrong problems still fail - one fixture per class that is cheap to construct

@pytest.mark.parametrize("axis, cls, quote, evidence, brief, measured", [
    # wrong scale: a millimetre part read as metres - the mesh is a thousand times the confirmed size
    ("domain_enclosure", "wrong_scale", "Reference length: 64.593 m", "iso", CRM_BRIEF,
     {**CRM_MEASURED, "body_length_m": 64593.0}),
    # a confirmed symmetry plane missing from a half model
    ("domain_enclosure", "wrong_boundaries", "symmetry as symmetry", "patch", CRM_BRIEF, None),
    # inlet and outlet swapped against the confirmed openings
    ("flow_passage_preserved", "wrong_flow_setup", "inlet as inlet; outlet_1 as outlet", "patch",
     PIPE_BRIEF, {}),
    # prism layers requested, zero anywhere
    ("prism_layer_coverage", "requirement_absent", "5 prism layers", "slice", CRM_BRIEF,
     {**CRM_MEASURED, "layer_coverage_pct": 0.0}),
    # a requested refinement zone left exactly at background size
    ("wake_resolution", "requirement_absent", "Local refinement at the leading edges ... and wake",
     "slice", CRM_BRIEF, None),
    # the wrong body: the cells fill the aircraft instead of the air around it
    ("domain_enclosure", "wrong_side_meshed", "Takeoff aero on this half-model airliner", "slice",
     CRM_BRIEF, None),
    # a component dropped from the mesh
    ("surface_capture", "missing_geometry", "half-model airliner", "patch", CRM_BRIEF, None),
    # cell bubbles inside the body
    ("domain_enclosure", "junk_regions", "enclose the half-model", "slice", CRM_BRIEF,
     {**CRM_MEASURED, "regions": 3}),
])
def test_each_wrong_problem_class_blocks(ledger, axis, cls, quote, evidence, brief, measured):
    L, ids = ledger
    j = _j(_f(axis, [ids[evidence]], wrong_problem=cls, brief_requirement=quote), L,
           brief=brief, measured=measured)
    assert j.blocking == cls, j.reason
    assert cls in WRONG_PROBLEM


def test_a_class_the_gates_measured_away_does_not_block(ledger):
    L, ids = ledger
    j = _j(_f("domain_enclosure", [ids["slice"]], wrong_problem="junk_regions",
              brief_requirement="enclose the half-model"), L)
    assert not j.blocking and "one connected region" in j.reason
    j = _j(_f("domain_enclosure", [ids["iso"]], wrong_problem="wrong_scale",
              brief_requirement="Reference length: 64.593 m"), L)
    assert not j.blocking and "matches the confirmed reference length" in j.reason


def test_a_wrong_problem_needs_an_inspection_not_a_gate_number(ledger):
    L, ids = ledger
    j = _j(_f("prism_layer_coverage", [ids["metric"]], wrong_problem="requirement_absent",
              brief_requirement="5 prism layers"), L,
           measured={**CRM_MEASURED, "layer_coverage_pct": 0.0})
    assert not j.blocking and "inspection" in j.reason


def test_an_unknown_class_is_a_concern(ledger):
    L, ids = ledger
    j = _j(_f("surface_capture", [ids["patch"]], wrong_problem="ugly_mesh",
              brief_requirement="half-model airliner"), L)
    assert not j.blocking


# asking for a rebuild

def test_a_cited_buildable_change_asks_for_a_rebuild(ledger):
    L, ids = ledger
    j = _j(_f("prism_layer_coverage", [ids["slice"]], brief_requirement="5 prism layers",
              builder_change="layers", change_request="thinner first layer"), L)
    assert j.improve and not j.blocking


@pytest.mark.parametrize("over, reason", [
    ({"builder_change": "none"}, "it names no change the builder can make"),
    ({"builder_change": "domain"}, "it names no change the builder can make"),  # not a lever here
    ({"change_request": ""}, "it does not say what to change"),
    ({"brief_requirement": "a dedicated wake region"}, "the requirement it quotes is not in your brief"),
])
def test_a_rebuild_needs_a_cited_lever_and_a_change(ledger, over, reason):
    L, ids = ledger
    kw = {"brief_requirement": "5 prism layers", "builder_change": "layers",
          "change_request": "thinner first layer", **over}
    j = _j(_f("prism_layer_coverage", [ids["slice"]], **kw), L,
           levers=("refinement", "layers", "boundaries"))
    assert not j.improve and j.reason == reason


def test_a_wrong_problem_may_also_ask_for_the_rebuild_that_fixes_it(ledger):
    L, ids = ledger
    j = _j(_f("prism_layer_coverage", [ids["slice"]], wrong_problem="requirement_absent",
              brief_requirement="5 prism layers", builder_change="layers",
              change_request="relax the layer quality controls so layers grow"), L,
           measured={**CRM_MEASURED, "layer_coverage_pct": 0.0})
    assert j.blocking == "requirement_absent" and j.improve


def test_a_passing_axis_is_not_judged(ledger):
    L, ids = ledger
    assert judge(AxisFinding("surface_capture", "ok", (ids["iso"],), passed=True),
                 brief_text=CRM_BRIEF, levers=SNAPPY_EXTERNAL, ledger=L) is None


# what the recorded findings mean downstream

def test_recorded_findings_ask_for_a_rebuild_only_when_judged_to():
    assert asks_for_rebuild([{"axis_key": "a", "passed": False, "improve": True}])
    assert not asks_for_rebuild([{"axis_key": "a", "passed": False, "improve": False}])
    # an unjudged record (in flight across the deploy) keeps the old meaning
    assert asks_for_rebuild([{"axis_key": "a", "passed": False}])
    assert asks_for_rebuild([])
    assert blocking_findings([{"axis_key": "a", "passed": False, "blocking": "wrong_scale"},
                              {"axis_key": "b", "passed": False, "blocking": ""},
                              {"axis_key": "c", "passed": False, "blocking": "made_up"}]) == [
        {"axis_key": "a", "passed": False, "blocking": "wrong_scale"}]
