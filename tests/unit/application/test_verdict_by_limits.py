# Responsibility: Verify the verdict by measured limits (docs/RULES.md VER-1..4): once it decides, a review cannot change a status, cause a rebuild, a bonus attempt or a retry, or fail a job; the verdict follows from the measured numbers alone and replays from the why record; in shadow it is only logged.
# Boundaries: routing after the review, terminal status, the closing message and record, the API readers, best-mesh ranking and the why record. Pure; nothing meshes.
from __future__ import annotations

import logging

import pytest

from meshpipeline.application import final_result as fr
from meshpipeline.application import review_delivery as rd
from meshpipeline.contracts import decision_record as dr
from meshpipeline.engines import verdict_limits as vl
from meshpipeline.pipeline import graph
from meshpipeline.settings import policy as polcfg

jlog = logging.getLogger(__name__)

#: a review that did not pass on two axes, one of them claiming the mesh is the wrong problem
#: with every judgement the old policy needed to fail the job, and one asking for a rebuild
FINDINGS = [
    {"axis_key": "surface_capture", "passed": True, "attempt": 0},
    {"axis_key": "prism_layer_coverage", "passed": False, "attempt": 0,
     "finding": "Layers cover only part of the wing.", "blocking": "", "improve": True,
     "severity": "minor"},
    {"axis_key": "wake_resolution", "passed": False, "attempt": 0,
     "finding": "The cells fill the solid.", "blocking": "wrong_side_meshed", "improve": False,
     "severity": "material"},
]


def _manifest(**quality) -> dict:
    q = {"max_non_ortho": 62.0, "skew_fraction": 0.0, "fatal": [], "layer_coverage_pct": 91.0,
         "regions": 1}
    q.update(quality)
    return {"quality": q, "patch_face_counts": {"aircraft": 12000, "farfield": 400},
            "patch_types": {"aircraft": "wall", "farfield": "patch"},
            "flow_topology": "external"}


def _state(**over) -> dict:
    s = {"job_id": "v1", "engine": "snappy", "purpose": "external_cfd",
         "flow_topology": "external", "engine_source": "suggested_confirmed",
         "intake_patches": [{"name": "aircraft", "type": "wall"},
                            {"name": "farfield", "type": "farfield"}],
         "request_txt": "Cruise aero on this wing, 5 prism layers.", "review_brief_txt": "",
         "api_failure": "", "executor_success": True, "executor_failed_gate": "",
         "reviewer_verdict": "FAIL", "retry_count": 0,
         "reviewer_axis_findings": [dict(f) for f in FINDINGS], "requirement_caveats": [],
         "user_dispute": {}, "engine_ladder": {}, "review_history": [],
         "mesh_manifest": _manifest()}
    s.update(over)
    return s


@pytest.fixture
def limits(monkeypatch):
    monkeypatch.setattr(polcfg, "VERDICT_BY_LIMITS", True)


@pytest.fixture
def shadow(monkeypatch):
    monkeypatch.setattr(polcfg, "VERDICT_BY_LIMITS", False)


def _final(state: dict, status: str = "succeeded") -> fr.FinalResult:
    outcome = fr.RunOutcome.from_graph_state(state)
    caveats = [c for c in (fr.review_concerns_caveat(outcome),
                           fr.review_inconclusive_caveat(outcome),
                           fr.measured_verdict_caveat(outcome),
                           fr.reviewer_notes_caveat(outcome)) if c]
    return fr.build_final_result(
        job_id="j", owner_id="o", status=fr.TerminalStatus(status), engine=state["engine"],
        purpose=state["purpose"], dimensionality="3D", approved_snapshot_id="",
        executor_success=bool(state.get("executor_success")),
        reviewer_verdict=state.get("reviewer_verdict", ""),
        failed_gate=state.get("executor_failed_gate", ""), api_failure=state.get("api_failure", ""),
        attempts=state["retry_count"], attempts_max=3, required_ready=status == "succeeded",
        delivered_types=["mesh_bundle"] if status == "succeeded" else [], optional_warnings=[],
        requirement_caveats=caveats, review_blocking=fr.review_blocking(outcome))


def _status(state: dict) -> fr.StatusDecision:
    return fr.derive_terminal_status(fr.RunOutcome.from_graph_state(state), job_id="j", jlog=jlog)


# a review can no longer cause a rebuild, a bonus attempt or a retry

@pytest.mark.parametrize("retry_count", [0, 1, 2])
def test_a_failing_review_ends_the_run_at_every_attempt(limits, retry_count):
    # attempt 1 (a rebuild), attempt 2 (the regular retry) and the old bonus attempt
    state = _state(retry_count=retry_count)
    assert graph.route_after_reviewer(state) == graph._END


def test_in_shadow_the_review_led_routing_still_decides(shadow):
    assert graph.route_after_reviewer(_state()) == "node_classifier"


def test_a_users_dispute_still_rebuilds(limits):
    # the user asked for the rebuild; the review only says what to look at
    state = _state(user_dispute={"mode": "rebuild"})
    assert graph.route_after_reviewer(state) == "node_classifier"


def test_a_review_that_could_not_finish_is_looked_at_again_or_ends(limits):
    state = _state(api_failure="<<API_FAILURE:reviewer_timeout>>", reviewer_verdict="")
    assert graph.route_after_reviewer(state) in ("node_review_retry", "node_failure_handler")


# a review can no longer change a status

def test_a_wrong_problem_claim_no_longer_fails_a_valid_mesh(limits):
    state = _state()
    assert _status(state).succeeded
    record = _final(state).to_dict()
    assert record["status"] == "succeeded"
    assert fr.review_outcome_of(record) == "passed"          # every measured limit is met
    assert fr.review_blocking(fr.RunOutcome.from_graph_state(state)) == []


def test_the_same_claim_failed_the_job_under_the_review_led_verdict(shadow):
    assert not _status(_state()).succeeded


def test_a_passing_review_cannot_hide_a_missed_limit(limits):
    state = _state(reviewer_verdict="PASS",
                   reviewer_axis_findings=[{"axis_key": "surface_capture", "passed": True}],
                   mesh_manifest=_manifest(max_non_ortho=74.2))
    assert _status(state).succeeded
    record = _final(state).to_dict()
    # a valid mesh past a limit: "Passed, with notes" on the job, as on the result screen
    assert fr.review_outcome_of(record) == "passed_with_notes"
    assert fr.review_concerns_of(record) == [
        "Worst face non-orthogonality: measured 74.2, limit at most 70"]
    msg = fr.render_message(_final(state))
    assert msg.startswith("Passed, with notes - these measured figures are past a limit:")
    assert "Verdict: passed, with notes (rule VER-2)" in msg


def test_the_reviewers_points_are_shown_as_labelled_notes(limits):
    record = _final(_state()).to_dict()
    notes = fr.review_notes_of(record)
    assert len(notes) == 2 and all(n.startswith("Reviewer note: ") for n in notes)
    assert any("The cells fill the solid." in n for n in notes)
    msg = fr.render_message(_final(_state()))
    assert "Reviewer notes (an AI reviewer looked at the mesh" in msg
    assert "Verdict: passed every measured limit (rule VER-3)" in msg
    assert "Review: delivered with concerns" not in msg


def test_a_review_that_did_not_finish_changes_nothing(limits):
    state = _state(api_failure="reviewer_timeout", reviewer_verdict="",
                   reviewer_axis_findings=[])
    assert _status(state).succeeded
    record = _final(state).to_dict()
    assert fr.review_outcome_of(record) == "passed"


def test_a_gate_failure_still_fails_the_job(limits):
    state = _state(executor_success=False, executor_failed_gate="quality_floor",
                   reviewer_verdict="", reviewer_axis_findings=[])
    assert not _status(state).succeeded


def test_a_review_ranks_nothing_between_two_meshes(limits):
    c = rd._measure(_state(), attempt=1, engine="snappy", workspace="", manifest=_manifest(),
                    entry={"verdict": "FAIL"}, current=False,
                    findings=[dict(f) for f in FINDINGS], caveats=[])
    assert c is not None and c.wrong_problem == 0 and c.review_left_concerns is False


# the verdict follows from the measured numbers, and replays

def test_the_verdict_is_reproducible_from_the_measured_numbers():
    a = vl.inputs_for(engine="snappy", topology="external", manifest=_manifest(max_non_ortho=71.0),
                      valid=True, layers_planned=True)
    b = vl.inputs_for(engine="snappy", topology="external", manifest=_manifest(max_non_ortho=71.0),
                      valid=True, layers_planned=True)
    assert a == b
    assert dr.verdict(a) == ("concerns", "VER-2")
    # nothing the review said is an input: the same numbers beside any notes give the same verdict
    c = vl.inputs_for(engine="snappy", topology="external", manifest=_manifest(max_non_ortho=71.0),
                      valid=True, layers_planned=True, notes=["anything at all"])
    assert dr.verdict(c) == dr.verdict(a)


@pytest.mark.parametrize(("quality", "layers", "expected"), [
    ({}, True, ("pass", "VER-3")),
    ({"layer_coverage_pct": 62.0}, True, ("concerns", "VER-2")),
    ({"layer_coverage_pct": 62.0}, False, ("pass", "VER-3")),     # no layers planned, no limit
    ({"regions": 2}, False, ("concerns", "VER-2")),
    ({"max_non_ortho": None}, True, ("pass", "VER-3")),           # unmeasured decides nothing
    ({"fatal": ["negative volume cells"]}, True, ("fail", "VER-1")),
])
def test_each_row_decides_by_its_limit(quality, layers, expected):
    inputs = vl.inputs_for(engine="snappy", topology="external", manifest=_manifest(**quality),
                           valid=True, layers_planned=layers)
    assert dr.verdict(inputs) == expected


def test_a_wrong_problem_is_named_only_from_a_measured_gate():
    inputs = vl.inputs_for(engine="snappy", topology="internal", manifest=_manifest(),
                           valid=False, failed_gate="port_areas")
    assert dr.verdict(inputs) == ("wrong_problem", "VER-4")
    inputs = vl.inputs_for(engine="snappy", topology="internal", manifest=_manifest(),
                           valid=False, failed_gate="quality_floor")
    assert dr.verdict(inputs) == ("fail", "VER-1")


def test_the_why_record_replays_the_verdict(limits):
    dr._CITED.clear()
    state = _state(mesh_manifest=_manifest(max_non_ortho=74.2))
    outcome = fr.RunOutcome.from_graph_state(state)
    cmp = fr.record_verdict("job-replay", outcome, succeeded=True, jlog=jlog)
    assert cmp["limits"] == "concerns" and cmp["decides"] is True
    rows = [e for e in dr.cited_for("job-replay") if e["stage"] == dr.STAGE_VERDICT]
    assert len(rows) == 1 and rows[0]["rule"] == "VER-2"
    assert dr.replay(rows) == []
    tampered = {**rows[0], "value": "pass", "rule": "VER-3"}
    assert dr.replay([tampered])                              # a record that lies is caught


def test_shadow_logs_both_verdicts_and_decides_nothing(shadow, caplog):
    dr._CITED.clear()
    state = _state()
    outcome = fr.RunOutcome.from_graph_state(state)
    decision = fr.derive_terminal_status(outcome, job_id="shadow", jlog=jlog)
    assert not decision.succeeded                              # the review-led verdict decides
    with caplog.at_level(logging.INFO):
        cmp = fr.record_verdict("shadow", outcome, succeeded=decision.succeeded, jlog=jlog)
    assert cmp == {"decides": False, "limits": "pass", "rule": "VER-3",
                   "review_led": "wrong_problem", "agree": False, "missed": []}
    assert any("verdict shadow:" in r.getMessage() for r in caplog.records)
    assert fr.measured_verdict_caveat(outcome) is None
    assert fr.reviewer_notes_caveat(outcome) is None
    entry = next(e for e in dr.cited_for("shadow") if e["stage"] == dr.STAGE_VERDICT)
    assert entry["inputs"]["decides"] is False and dr.replay([entry]) == []


# the limits table

def test_every_limit_names_a_known_engine_and_a_source():
    from meshpipeline.engines.registry import engine_names
    known = set(engine_names())
    for lim in vl.PRODUCTION_LIMITS:
        assert set(lim.engines) <= known, lim
        assert lim.source, lim
        assert lim.op in ("<=", ">=", "<", ">", "==", "empty"), lim


def test_every_wrong_problem_class_has_a_row():
    from meshpipeline.agents.reviewer.review_policy import WRONG_PROBLEM
    assert set(dr.WRONG_PROBLEM_CHECKS) == set(WRONG_PROBLEM)


# AN FEA JOB TELLS ONE STORY (22 Oct: nine valid FEA meshes read "Review: passed" on the job while
# their result screens read "with notes"): its measured limits decide even in shadow, over the same
# rows the result screen shows

def _fea_state(**quality) -> dict:
    q = {"fea": {"plan": True, "through_wall_min": 2.4, "rules": {"short": []}}, "element_order": 2,
         "fea_inverted": 0, "elements_under_floor": 0, "sicn_floor": 0.01, "fatal": [],
         "fea_min_scaled_jacobian": 0.3, "fea_max_jacobian_ratio": 1.5, "fea_max_aspect_ratio": 4.0,
         "fea_min_dihedral_deg": 12.0, "fea_max_dihedral_deg": 150.0, "fea_rules_short": 0}
    q.update(quality)
    return _state(engine="gmsh", purpose="structural", flow_topology="", reviewer_verdict="PASS",
                  reviewer_axis_findings=[{"axis_key": "right_part", "passed": True}],
                  mesh_manifest={"quality": q})


def test_an_fea_mesh_with_notes_reads_passed_with_notes_on_the_job_too(shadow):
    state = _fea_state(fea_max_aspect_ratio=31.0)
    assert _status(state).succeeded
    record = _final(state).to_dict()
    assert fr.review_outcome_of(record) == "passed_with_notes"
    msg = fr.render_message(_final(state))
    assert "Verdict: passed, with notes" in msg and "Review: passed" not in msg
    assert any("aspect" in c.lower() for c in fr.review_concerns_of(record))


def test_an_fea_mesh_without_notes_is_a_plain_pass_in_shadow_too(shadow):
    record = _final(_fea_state()).to_dict()
    assert fr.review_outcome_of(record) == "passed"
    assert "Verdict: passed every measured limit" in fr.render_message(_final(_fea_state()))


def test_the_result_screen_and_the_verdict_read_the_same_rows():
    from meshpipeline.engines import verdict_limits as vl

    q = _fea_state(elements_under_floor=4)["mesh_manifest"]["quality"]
    q["fea"]["through_wall_min"] = 1.5
    keys = [r["key"] for r in vl.fea_limits(q)]
    assert keys[:2] == ["elements_under_floor", "through_wall_min"]
    missed = {r["key"] for r in vl.fea_limits(q) if not dr.limit_met(r["measured"], r["op"], r["limit"])}
    assert {"elements_under_floor", "through_wall_min"} <= missed


def test_an_fea_result_names_no_detail_level():
    from types import SimpleNamespace

    cfd = SimpleNamespace(purpose="external_cfd", effective_mesh_fidelity="standard", mesh_fidelity_source="default")
    fea = SimpleNamespace(purpose="structural", effective_mesh_fidelity="standard", mesh_fidelity_source="default")
    assert "Standard" in fr._render_fidelity(cfd)
    assert fr._render_fidelity(fea) == ""
