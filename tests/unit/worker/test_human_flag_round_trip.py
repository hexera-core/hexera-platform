# Responsibility: Drive the whole human-flag loop through the production contracts, nodes and gates.
# Boundaries: only the model's tool arguments are doubles; every state, route, rubric and gate below is production.
from __future__ import annotations

import asyncio
import json
import types

import pytest

from meshpipeline.agents.loop.accounting import ToolInvocation
from meshpipeline.agents.reviewer.eligibility import Eligibility
from meshpipeline.agents.reviewer.loop_policy import ReviewLoopPolicy
from meshpipeline.contracts import human_flags as HF
from meshpipeline.contracts.agent_loop import LoopLimits
from meshpipeline.contracts.evidence_ledger import EvidenceLedger, EvidenceStatus

_FLAGS = [
    {"x": 0.9, "y": 0.1, "z": 0.0, "span": 0.05, "patch": "wing",
     "note": "layers look collapsed at the wing root"},
    {"x": -0.2, "y": 0.4, "z": 0.1, "span": 0.02, "patch": "fuselage",
     "note": "cells are far too coarse here"},
]
_DISPUTE = {"of_job_id": "parent-1", "mode": "rebuild",
            "comment": "wing root looks wrong and the fuselage is under-resolved",
            "flags": _FLAGS}


def _plan(axes):
    # The AXES ARE COMPOSED BY PRODUCTION - including, when a dispute is live, the conditional
    # human-feedback axis. Only the surrounding requirement sets are narrowed, so the policy under
    # test is exercised on eligibility and the flag gate rather than on render obligations.
    return types.SimpleNamespace(
        axes=axes, engine="gmsh", purpose="external_cfd",
        required_gate_keys=frozenset(), required_metric_keys=frozenset(),
        required_render_artifacts=frozenset(), required_targets=())


def _policy(axes, *, user_dispute, phase):
    # REAL evidence in a REAL ledger: eligibility refuses a finding that cites nothing it produced,
    # and that refusal is production behaviour this test must not paper over.
    ledger = EvidenceLedger()
    evidence_id = ledger.add_render_view("iso", "opening", "open", image_ok=True)
    # A NOT-passed axis must cite something that grounds a defect - production refuses an
    # unsupported failure, and this test must satisfy that bar rather than route around it.
    defect_id = ledger.add_metric("layer_coverage", 0.2, False, EvidenceStatus.FAIL,
                                  "layer coverage below the floor", "measure")
    policy = ReviewLoopPolicy(plan=_plan(axes), ledger=ledger, runtime=None,
                              limits_=LoopLimits(max_rounds=30, no_progress_threshold=3),
                              user_dispute=user_dispute, dispute_phase=phase)
    policy.evidence_id = evidence_id
    policy.defect_id = defect_id
    return policy


def _submit(policy, axes, flag_rows, *, passed=True):
    cited = [policy.evidence_id] if passed else [policy.evidence_id, policy.defect_id]
    args = {
        "axis_findings": [{"axis_key": a.name, "finding": "inspected", "evidence_ids": cited,
                           "passed": passed} for a in axes],
        "rebuild_required": False,
        "reasoning": "report",
    }
    if flag_rows is not None:
        args["flag_findings"] = flag_rows
    inv = ToolInvocation(round_index=1, call_index=1, tool="submit_findings",
                         raw_arguments=json.dumps(args), parsed=args)
    return asyncio.run(policy.execute(inv))


def _axes(user_dispute, phase):
    # ONE ordinary axis plus, when a human flagged regions, the PRODUCTION human-feedback axis
    # appended by the shared rubric authority. The full engine rubric is deliberately not composed
    # here: its per-axis grounding rules are a different subject with their own suite, and this
    # test is about the flag contract. That the real rubric appends this axis - and appends it only
    # for a dispute - is proven in test_human_flag_rereview.py.
    import dataclasses

    from meshpipeline.contracts.human_flags import expected_ordinals
    from meshpipeline.engines.quality_criteria import human_feedback_axis

    base = types.SimpleNamespace(name="mesh_quality", owner="engine:gmsh", requires=(),
                                 validation_axis="quality")
    if not expected_ordinals(user_dispute):
        return (base,)
    return (base, dataclasses.replace(human_feedback_axis(phase), owner="human:dispute"))


# the round trip

def test_the_complete_human_flag_round_trip_through_production_contracts(tmp_path):
    from meshpipeline.agents.builder.attempt import flag_responses, human_rebuild_requirements
    from meshpipeline.agents.builder.tool_context import BuilderToolContext
    from meshpipeline.agents.builder.tools.meshing import submit_mesh
    from meshpipeline.application.dispatch_contract import build as build_dispatch
    from meshpipeline.application.dispatch_contract import to_run_kwargs
    from meshpipeline.pipeline.classifier import node_classifier
    from meshpipeline.pipeline.enums import Verdict
    from meshpipeline.pipeline.graph import route_after_engine_select, route_after_reviewer
    from meshpipeline.pipeline.state_factory import make_pipeline_state

    # 1. DISPATCH: the route's payload, through the JSONB round trip a hosted run reconstructs from
    payload = build_dispatch(job_id="j-1", owner_id="o", geometry_source=None,
                             geometry_interpretation=None, session_id="", request_txt="",
                             review_brief_txt="", intake_patches=[], dimensionality="",
                             purpose="external_cfd", input_kind="", user_dispute=_DISPUTE)
    kwargs = to_run_kwargs(json.loads(json.dumps(payload)), where="round trip")
    assert kwargs["user_dispute"] == _DISPUTE

    # 2. STATE: built by the production factory from exactly those kwargs
    state = make_pipeline_state(job_id="j-1", user_id="o", request_txt="",
                                purpose="external_cfd",
                                user_dispute=kwargs["user_dispute"])
    assert state["dispute_flag_findings"] == [] and state["builder_flag_responses"] == []

    # 3. ROUTE: a dispute re-reviews the disputed mesh before anything is rebuilt
    assert route_after_engine_select(state) == "node_reviewer"

    # 4. FIRST REVIEW: the composed rubric carries the conditional axis, and the policy demands a
    #    baseline for every flag before it will accept a submission at all.
    parent_axes = _axes(state["user_dispute"], HF.PHASE_PARENT)
    assert "human_flagged_regions" in {a.name for a in parent_axes}
    p1 = _policy(parent_axes, user_dispute=state["user_dispute"], phase=HF.PHASE_PARENT)
    assert _submit(p1, parent_axes, None).accepted is False, "a dispute review may not skip the flags"

    baseline_rows = [
        {"ordinal": 1, "status": "confirmed", "observation": "1 layer of 5 at the root",
         "explanation": "measured on the disputed mesh", "measurements": "nLayers=1"},
        {"ordinal": 2, "status": "not_confirmed", "observation": "4mm cells, as briefed",
         "explanation": "measured on the disputed mesh", "measurements": "4mm"},
    ]
    out1 = _submit(p1, parent_axes, baseline_rows, passed=False)
    assert out1.accepted is True and p1.accepted is not None
    state["reviewer_verdict"] = Verdict.FAIL
    state["reviewer_feedback"] = "the wing root layers are collapsed"
    state["reviewer_axis_findings"] = [
        {"axis_key": f.axis_key, "passed": f.passed} for f in p1.accepted_findings]
    # the Reviewer's own write surface carries the baseline forward
    state["dispute_flag_findings"] = HF.as_dicts(p1.accepted_flag_findings)
    assert len(state["dispute_flag_findings"]) == 2

    # 5. ROUTE: a rebuild-mode dispute always goes on to rebuild, whatever the verdict was
    assert route_after_reviewer(state) == "node_classifier"

    # 6. CLASSIFIER: production node, on the real state
    state.update(asyncio.run(node_classifier({**state, "executor_success": True,
                                              "engine": "gmsh", "retry_count": 0})))
    assert state["classifier_result"]["error_source"] == "reviewer_fail"

    # 7. BUILDER: receives the human's own words, coordinates and the recorded baseline - none of
    #    it via the classifier's summary
    state["retry_count"] = 1
    block = human_rebuild_requirements(state)
    assert "wing root looks wrong and the fuselage is under-resolved" in block
    assert "x=0.9" in block and "patch wing" in block
    assert "layers look collapsed at the wing root" in block
    assert "found it confirmed" in block and "nLayers=1" in block
    assert "UNTRUSTED DATA" in block

    # 8. BUILDER DECLARATION: submit_mesh refuses to finish until every flag is answered
    (tmp_path / "mesh.inp").write_text("x")
    ctx = BuilderToolContext(workspace=tmp_path, geometry=None, engine="gmsh",
                             user_dispute=state["user_dispute"])
    assert submit_mesh(ctx, {})["success"] is False
    assert submit_mesh(ctx, {"flag_responses": [
        {"ordinal": 1, "intended_correction": "restore the boundary layer",
         "change_made": "nSurfaceLayers 1 -> 5", "affected_region": "wing",
         "believed_addressed": True},
        {"ordinal": 2, "intended_correction": "none", "change_made": "nothing - baseline clear",
         "affected_region": "", "believed_addressed": False}]})["success"] is True
    state["builder_flag_responses"] = flag_responses(tmp_path)
    assert len(state["builder_flag_responses"]) == 2

    # 9. SECOND REVIEW: the rebuilt phase, carrying the baseline and the builder's claim
    rebuilt_axes = _axes(state["user_dispute"], HF.PHASE_REBUILT)
    p2 = _policy(rebuilt_axes, user_dispute=state["user_dispute"], phase=HF.PHASE_REBUILT)

    # 9a. THE GATE: a PASS that leaves a flag unresolved is refused, not accepted
    refused = _submit(p2, rebuilt_axes, [
        {"ordinal": 1, "status": "unresolved", "observation": "still 1 layer",
         "explanation": "measured", "measurements": "nLayers=1"},
        {"ordinal": 2, "status": "not_reproduced", "observation": "4mm", "explanation": "measured",
         "measurements": "4mm"}], passed=True)
    assert refused.accepted is False
    assert "flag 1 is unresolved" in refused.content
    assert p2.accepted is None, "an unresolved flag must not yield a verdict"

    # 9b. the honest answers pass: the rebuild fixed one and never reproduced the other
    accepted = _submit(p2, rebuilt_axes, [
        {"ordinal": 1, "status": "resolved", "observation": "5 layers at the root",
         "explanation": "the builder's change is present and measured", "measurements": "nLayers=5"},
        {"ordinal": 2, "status": "not_reproduced", "observation": "4mm cells",
         "explanation": "as at baseline", "measurements": "4mm"}], passed=True)
    assert accepted.accepted is True
    assert p2.accepted.outcome is Eligibility.PASS
    assert [f.ordinal for f in p2.accepted_flag_findings] == [1, 2]


# the gate, case by case

@pytest.mark.parametrize("rows, why", [
    (None, "no flag results at all"),
    ([], "an empty list"),
    ([{"ordinal": 1, "status": "resolved", "observation": "o", "explanation": "e",
       "measurements": "m"}], "one flag omitted"),
    ([{"ordinal": 1, "status": "resolved", "observation": "o", "explanation": "e",
       "measurements": "m"},
      {"ordinal": 1, "status": "resolved", "observation": "o", "explanation": "e",
       "measurements": "m"}], "a duplicated ordinal"),
    ([{"ordinal": 1, "status": "resolved", "observation": "o", "explanation": "e",
       "measurements": "m"},
      {"ordinal": 7, "status": "resolved", "observation": "o", "explanation": "e",
       "measurements": "m"}], "an ordinal nobody raised"),
    ([{"ordinal": 1, "status": "unassessable", "observation": "could not get there",
       "explanation": "e", "measurements": ""},
      {"ordinal": 2, "status": "resolved", "observation": "o", "explanation": "e",
       "measurements": "m"}], "an unassessable flag"),
])
def test_a_pass_is_impossible_when(rows, why):
    axes = _axes(_DISPUTE, HF.PHASE_REBUILT)
    p = _policy(axes, user_dispute=_DISPUTE, phase=HF.PHASE_REBUILT)
    out = _submit(p, axes, rows, passed=True)
    assert out.accepted is False, why
    assert p.accepted is None, f"a verdict was produced despite {why}"


def test_an_ordinary_review_is_completely_unaffected():
    # No dispute: the composed rubric is the historical one, no flag property is offered, and a
    # submission with no flag results is accepted exactly as before.
    axes = _axes(None, "")
    assert "human_flagged_regions" not in {a.name for a in axes}
    p = _policy(axes, user_dispute=None, phase="")
    out = _submit(p, axes, None, passed=True)
    assert out.accepted is True
    assert p.accepted.outcome is Eligibility.PASS
    assert p.accepted_flag_findings == ()


def test_a_failing_verdict_is_never_blocked_by_the_flag_gate():
    # A FAIL that names an unresolved region is the correct answer, and must reach the builder.
    axes = _axes(_DISPUTE, HF.PHASE_REBUILT)
    p = _policy(axes, user_dispute=_DISPUTE, phase=HF.PHASE_REBUILT)
    out = _submit(p, axes, [
        {"ordinal": 1, "status": "unresolved", "observation": "still collapsed",
         "explanation": "measured", "measurements": "nLayers=1"},
        {"ordinal": 2, "status": "resolved", "observation": "o", "explanation": "e",
         "measurements": "m"}], passed=False)
    assert out.accepted is True
    assert p.accepted.outcome is Eligibility.FAIL
