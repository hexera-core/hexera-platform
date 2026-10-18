# Responsibility: Verify a run with a gate-passing mesh is delivered unless the review proved it the wrong problem, and is never reported as a pass it did not get.
# Boundaries: terminal status, the closing message and record, the API's review outcome, best-mesh selection, routing and the identical-evidence memo.
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from meshpipeline.application import final_result as fr
from meshpipeline.application.review_delivery import select
from meshpipeline.pipeline import engine_fallback as lad

jlog = logging.getLogger(__name__)

# e0fa8ad0's last review, as recorded once its findings are judged: two concerns, no wrong problem
CRM_FINDINGS = [
    {"axis_key": "surface_capture", "passed": True, "attempt": 2},
    {"axis_key": "prism_layer_coverage", "passed": False, "attempt": 2,
     "finding": "Aircraft coverage is only 57.1%, averaging 2.4 of the requested 5 layers.",
     "blocking": "", "improve": False, "severity": "minor",
     "concern_reason": "it cites no requirement from your brief"},
    {"axis_key": "wake_resolution", "passed": False, "attempt": 2,
     "finding": "No distinct downstream wake refinement region.", "blocking": "",
     "improve": False, "severity": "minor",
     "concern_reason": "it cites no requirement from your brief"},
]


def _state(**over) -> dict:
    s = {"job_id": "e0fa8ad0", "engine": "snappy", "purpose": "external_cfd",
         "engine_source": "suggested_confirmed", "input_kind": "solid-body", "dimensionality": "3D",
         "intake_patches": [{"name": "aircraft", "type": "wall"},
                            {"name": "farfield", "type": "farfield"},
                            {"name": "symmetry", "type": "symmetry"}],
         "request_txt": "Takeoff aero on this half-model airliner, 70 m/s, 5 prism layers.",
         "review_brief_txt": "", "api_failure": "", "executor_success": True,
         "executor_failed_gate": "", "reviewer_verdict": "FAIL", "retry_count": 2,
         "reviewer_axis_findings": [dict(f) for f in CRM_FINDINGS], "requirement_caveats": [],
         "user_dispute": {}, "engine_ladder": {}, "review_history": []}
    s.update(over)
    return s


def _final(state: dict, status: str) -> fr.FinalResult:
    outcome = fr.RunOutcome.from_graph_state(state)
    caveats = [c for c in (fr.review_concerns_caveat(outcome), fr.review_inconclusive_caveat(outcome))
               if c]
    result = fr.build_final_result(
        job_id="j", owner_id="o", status=fr.TerminalStatus(status), engine=state["engine"],
        purpose=state["purpose"], dimensionality="3D", approved_snapshot_id="",
        executor_success=bool(state.get("executor_success")),
        reviewer_verdict=state.get("reviewer_verdict", ""),
        failed_gate=state.get("executor_failed_gate", ""), api_failure=state.get("api_failure", ""),
        attempts=state["retry_count"], attempts_max=3, required_ready=status == "succeeded",
        delivered_types=["mesh_bundle"] if status == "succeeded" else [], optional_warnings=[],
        requirement_caveats=caveats, review_blocking=fr.review_blocking(outcome))
    return fr.with_engine_ladder(result, lad.final_record(state, succeeded=status == "succeeded",
                                                          system_failure=False))


# a gate-passing mesh with a reviewer FAIL is DELIVERED, with the concerns listed

def test_tonights_crm_run_is_delivered_with_its_concerns():
    state = _state()
    decision = fr.derive_terminal_status(fr.RunOutcome.from_graph_state(state), job_id="j",
                                         jlog=jlog)
    assert decision.succeeded
    result = _final(state, "succeeded")
    msg = fr.render_message(result)
    assert msg.startswith("Delivered with the reviewer's concerns:")
    assert "boundary layer does not cover enough of the wall" in msg      # the axis, in words
    assert "No distinct downstream wake refinement region" in msg          # the reviewer's own
    assert "Review: delivered with concerns (see above)" in msg
    assert "Review: passed" not in msg                                     # never a fake PASS
    assert "Mesh generation completed successfully." in msg
    record = result.to_dict()
    assert record["reviewer_verdict"] == "failed" and record["status"] == "succeeded"
    assert fr.review_outcome_of(record) == "delivered_with_concerns"
    assert len(fr.review_concerns_of(record)) == 2


def test_a_material_concern_is_shown_first_and_marked():
    findings = [dict(f) for f in CRM_FINDINGS]
    findings[2]["severity"] = "material"
    msg = fr.render_message(_final(_state(reviewer_axis_findings=findings), "succeeded"))
    lines = msg.splitlines()
    assert lines[1].startswith("  - IMPORTANT - ")
    assert "wake behind the body is not resolved" in lines[1]


def test_a_clean_pass_is_still_a_plain_pass():
    state = _state(reviewer_verdict="PASS",
                   reviewer_axis_findings=[{"axis_key": "surface_capture", "passed": True}])
    result = _final(state, "succeeded")
    assert "Review: passed" in fr.render_message(result)
    assert fr.review_outcome_of(result.to_dict()) == "passed"


def test_succeeded_beside_a_fail_still_requires_the_stated_concerns():
    with pytest.raises(ValueError):
        fr.build_final_result(
            job_id="j", owner_id="o", status=fr.TerminalStatus.succeeded, engine="snappy",
            purpose="external_cfd", dimensionality="3D", approved_snapshot_id="",
            executor_success=True, reviewer_verdict="FAIL", failed_gate="", api_failure="",
            attempts=2, attempts_max=3, required_ready=True, delivered_types=["mesh"],
            optional_warnings=[], requirement_caveats=[])


# only the gates - or a confirmed wrong problem - fail a job

def test_a_gate_failure_still_fails_the_job():
    state = _state(executor_success=False, executor_failed_gate="quality_floor",
                   reviewer_verdict="", reviewer_axis_findings=[])
    assert select(state) == state
    assert not fr.derive_terminal_status(fr.RunOutcome.from_graph_state(state), job_id="j",
                                         jlog=jlog).succeeded


def test_a_confirmed_wrong_problem_fails_the_job_and_says_which():
    findings = [dict(f) for f in CRM_FINDINGS]
    findings[1].update({"blocking": "requirement_absent",
                        "finding": "No prism layers anywhere on the aircraft wall."})
    state = _state(reviewer_axis_findings=findings)
    decision = fr.derive_terminal_status(fr.RunOutcome.from_graph_state(state), job_id="j",
                                         jlog=jlog)
    assert not decision.succeeded and decision.failed_reason.value == "reviewer_rejected"
    result = _final(state, "failed")
    msg = fr.render_message(result)
    assert "represents a different problem than the one you confirmed" in msg
    assert "Something the brief requires is entirely absent from the mesh" in msg
    assert "No prism layers anywhere on the aircraft wall" in msg
    assert fr.review_outcome_of(result.to_dict()) == "wrong_problem"


def test_an_unfinished_review_keeps_its_own_delivery():
    state = _state(api_failure="reviewer_stalled", reviewer_verdict="FAIL",
                   review_history=[{"attempt": 1, "workspace": "/nowhere", "verdict": "PASS",
                                    "findings": []}])
    assert select(state) == state                       # never swapped away from
    outcome = fr.RunOutcome.from_graph_state(state)
    assert fr.review_inconclusive_caveat(outcome)["kind"] == fr.REVIEW_INCONCLUSIVE
    assert fr.review_concerns_caveat(outcome) is None
    assert fr.derive_terminal_status(outcome, job_id="j", jlog=jlog).succeeded


# the best gate-passing mesh is delivered when a later attempt did worse

def _attempt(tmp_path: Path, n: int, findings: list, verdict: str = "FAIL") -> dict:
    ws = tmp_path / f"attempt_{n}"
    ws.mkdir()
    (ws / "mesh_manifest.json").write_text(json.dumps({"cell_count": 1000 + n}))
    return {"attempt": n, "workspace": str(ws), "verdict": verdict, "findings": findings,
            "feedback": "", "requirement_caveats": [], "evidence_key": f"k{n}"}


def test_a_later_gate_failure_delivers_the_earlier_reviewed_mesh(tmp_path):
    first = _attempt(tmp_path, 1, [dict(f) for f in CRM_FINDINGS])
    state = _state(executor_success=False, executor_failed_gate="quality_floor",
                   openfoam_workspace=str(tmp_path / "attempt_3"), review_history=[first],
                   reviewer_axis_findings=[], reviewer_verdict="FAIL", retry_count=3)
    picked = select(state)
    assert picked["openfoam_workspace"] == first["workspace"]
    assert picked["executor_success"] is True and picked["executor_failed_gate"] == ""
    assert picked["mesh_manifest"]["cell_count"] == 1001
    assert picked["retry_count"] == 3                   # the attempts made stay honest
    assert fr.derive_terminal_status(fr.RunOutcome.from_graph_state(picked), job_id="j",
                                     jlog=jlog).succeeded


def test_the_better_reviewed_mesh_wins_and_ties_go_to_the_latest(tmp_path):
    clean = _attempt(tmp_path, 1, [dict(CRM_FINDINGS[1])])
    worse = _attempt(tmp_path, 2, [dict(CRM_FINDINGS[1], blocking="wrong_scale")])
    state = _state(openfoam_workspace=worse["workspace"], review_history=[clean, worse],
                   reviewer_axis_findings=worse["findings"])
    assert select(state)["openfoam_workspace"] == clean["workspace"]
    same = _attempt(tmp_path, 3, [dict(CRM_FINDINGS[1])])
    state = _state(openfoam_workspace=same["workspace"], review_history=[clean, same],
                   reviewer_axis_findings=same["findings"])
    assert select(state) == state


def test_an_unreadable_earlier_mesh_keeps_the_runs_own_outcome(tmp_path):
    gone = {"attempt": 1, "workspace": str(tmp_path / "missing"), "verdict": "FAIL",
            "findings": []}
    state = _state(executor_success=False, executor_failed_gate="quality_floor",
                   review_history=[gone])
    assert select(state) == state


# the optional improvement rides on the delivery, never instead of it

def test_a_delivery_whose_open_points_are_all_layers_offers_fewer_layers_optionally():
    findings = [dict(CRM_FINDINGS[0]), dict(CRM_FINDINGS[1])]
    msg = fr.render_message(_final(_state(reviewer_axis_findings=findings), "succeeded"))
    assert "Optional: the review's open points are all about the 5 near-wall layers" in msg
    assert 'Reply "use 2 layers"' in msg
    # with the wake also open, fewer layers is no answer, and nothing is offered
    assert "use 2 layers" not in fr.render_message(_final(_state(), "succeeded"))


# routing: a rebuild only for a cited, buildable change

def test_the_review_only_rebuilds_for_a_buildable_change():
    from meshpipeline.pipeline.graph import route_after_reviewer
    base = {"job_id": "j", "reviewer_verdict": "FAIL", "retry_count": 1, "executor_success": True}
    assert route_after_reviewer({**base, "reviewer_axis_findings": CRM_FINDINGS}) == "__end__"
    asks = [dict(CRM_FINDINGS[1], improve=True)]
    assert route_after_reviewer({**base, "reviewer_axis_findings": asks}) == "node_classifier"
    # spent: no attempt left, the run ends and the mesh is delivered
    assert route_after_reviewer({**base, "retry_count": 3,
                                 "reviewer_axis_findings": asks}) == "__end__"


def test_the_builder_is_handed_the_asks_not_the_concerns():
    from meshpipeline.pipeline.classifier import failed_axes
    rows = [dict(CRM_FINDINGS[1], improve=True), dict(CRM_FINDINGS[2])]
    assert failed_axes(rows) == ["prism_layer_coverage"]
    assert failed_axes([{"axis_key": "a", "passed": False}]) == ["a"]   # unjudged: as before


# the same mesh with the same evidence is never judged twice

def test_identical_evidence_has_one_key_and_a_changed_mesh_another(tmp_path):
    from meshpipeline.agents.reviewer.visual import _evidence_key
    from meshpipeline.engines.registry import get_spec
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "snappyHexMeshDict").write_text("layers 5")
    args = {"engine": "snappy", "purpose": "external_cfd", "request": "r", "brief": "b",
            "confirmed": "c", "workspace": tmp_path, "spec": get_spec("snappy"),
            "manifest": {"quality": {"layer_coverage_pct": 48.0194}, "cell_count": 1219085}}
    key = _evidence_key(**args)
    assert key == _evidence_key(**args)
    assert key != _evidence_key(**{**args, "manifest": {"quality": {"layer_coverage_pct": 57.1},
                                                         "cell_count": 1219085}})
    (tmp_path / "system" / "snappyHexMeshDict").write_text("layers 3")
    assert key != _evidence_key(**args)


def test_a_reused_review_keeps_its_verdict_but_asks_for_nothing_again(tmp_path):
    from meshpipeline.agents.reviewer.visual import _reuse_review
    entry = {"attempt": 2, "workspace": str(tmp_path), "verdict": "FAIL", "feedback": "f",
             "evidence_key": "k",
             "findings": [dict(CRM_FINDINGS[1], improve=True),
                          dict(CRM_FINDINGS[2], blocking="requirement_absent")]}
    out = _reuse_review(entry, retry_count=3, workspace=tmp_path, history=(entry,),
                        evidence_key="k", requirement_caveats=[])
    assert out["reviewer_verdict"] == "FAIL"
    assert all(f["attempt"] == 3 and f["improve"] is False
               for f in out["reviewer_axis_findings"] if f["passed"] is False)
    assert out["reviewer_axis_findings"][1]["blocking"] == "requirement_absent"
    assert len(out["review_history"]) == 2
