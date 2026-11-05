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


@pytest.mark.parametrize("marker", ["<<API_FAILURE:reviewer_timeout>>", "reviewer_stalled",
                                    "<<API_FAILURE:reviewer_non_transient>>", "reviewer_crashed"])
def test_a_review_that_could_not_finish_ends_the_run_and_the_limits_decide(limits, marker):
    # VER-5: no review rerun and no failure because the model was busy, down or gave no answer
    state = _state(api_failure=marker, reviewer_verdict="", reviewer_axis_findings=[])
    assert graph.route_after_reviewer(state) == graph._END
    outcome = fr.RunOutcome.from_graph_state(state)
    assert fr.review_blocking(outcome) == []


def test_in_shadow_a_review_that_could_not_finish_is_looked_at_again_or_ends(shadow):
    state = _state(api_failure="<<API_FAILURE:reviewer_timeout>>", reviewer_verdict="")
    assert graph.route_after_reviewer(state) in ("node_review_retry", "node_failure_handler")


def test_the_review_route_is_in_the_why_record(limits):
    dr._CITED.pop("v5-why", None)
    graph.route_after_reviewer(_state(job_id="v5-why", api_failure="reviewer_stalled",
                                      reviewer_verdict=""))
    rows = [e for e in dr.cited_for("v5-why") if e["stage"] == "review_route"]
    assert rows and rows[0]["rule"] == "VER-5" and rows[0]["value"] == "end"
    assert rows[0]["inputs"]["review_concluded"] is False


def test_a_crash_after_a_failing_review_is_rescued_once_the_limits_decide(limits):
    from meshpipeline.application import post_gate_rescue as pgr
    state = _state(reviewer_rebuild_required=True)
    rescued = pgr.rescue(state, RuntimeError("checkpoint write"))
    assert rescued is not None and rescued["reviewer_verdict"] == "FAIL"
    # a dispute still needs the engineer's own judgement
    assert pgr.rescue(_state(user_dispute={"mode": "rebuild"}), RuntimeError("x")) is None


def test_in_shadow_a_crash_after_a_failing_review_is_not_rescued(shadow):
    from meshpipeline.application import post_gate_rescue as pgr
    assert pgr.rescue(_state(), RuntimeError("checkpoint write")) is None


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
    # the layer point is settled by the measured coverage (VER-7); the wake point stays a note
    assert len(notes) == 1 and all(n.startswith("Reviewer note: ") for n in notes)
    assert any("The cells fill the solid." in n for n in notes)
    assert not any("Layers cover only part" in n for n in notes)
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


def test_an_fea_mesh_with_elements_under_the_note_floor_is_delivered_with_notes(shadow):
    # dev-areen 24 Oct: a valid chair assembly, 7 curved slivers under SICN 0.01, was refused by the
    # gmsh gating row - for FEA that row is a note (validity is the only blocker)
    state = _fea_state(elements_under_floor=7, fea_min_scaled_jacobian=7.5e-05)
    assert _status(state).succeeded
    assert not [r for r in vl.gating_rows("gmsh", state["mesh_manifest"]) if r["key"] == "elements_under_floor"]
    assert fr.review_outcome_of(_final(state).to_dict()) == "passed_with_notes"


def test_a_degenerate_fea_element_still_fails(shadow):
    state = _fea_state(fatal=["degenerate elements (SICN <= 0)"])
    assert any(r["key"] == "fatal" for r in vl.gating_rows("gmsh", state["mesh_manifest"]))


def test_a_flow_mesh_keeps_its_quality_floor_gate():
    rows = vl.gating_rows("gmsh", _manifest(elements_under_floor=3))
    assert any(r["key"] == "elements_under_floor" for r in rows)


# VER-6 / VER-7: job fb44fed7 - a valid FEA mesh recorded the model's FAIL over a point the
# measurements settle. The general pattern: once the limits decide, the recorded review verdict is
# theirs, and a point a measurement settles is not an open point.

def test_once_the_limits_decide_the_recorded_review_verdict_is_the_measured_one(limits):
    outcome = fr.RunOutcome.from_graph_state(_state())
    assert outcome.reviewer_verdict == "FAIL"
    assert fr.recorded_review_verdict(outcome) == "PASS"
    notes = fr.reviewer_notes_caveat(outcome)
    assert notes["model_verdict"] == "FAIL" and notes["verdict_rule"] == "VER-6"


def test_in_shadow_the_recorded_review_verdict_is_the_reviews_own(shadow):
    outcome = fr.RunOutcome.from_graph_state(_state())
    assert fr.recorded_review_verdict(outcome) == "FAIL"


def test_an_invalid_mesh_keeps_the_reviews_own_verdict(limits):
    outcome = fr.RunOutcome.from_graph_state(_state(executor_success=False,
                                                    executor_failed_gate="checkmesh"))
    assert fr.recorded_review_verdict(outcome) == "FAIL"


def test_a_point_a_measurement_settles_is_not_an_open_point(limits):
    # the review says the layers cover only part of the wing; the measured coverage meets its limit
    outcome = fr.RunOutcome.from_graph_state(_state())
    rows = fr._verdict_rows(outcome)
    cov = rows.get("layer_coverage_pct")
    assert cov is not None and cov.get("measured") == 91.0
    by = fr.settled_by_measurement(outcome, "prism_layer_coverage", rows)
    notes = fr.reviewer_notes_caveat(outcome)
    open_keys = [i["axis_key"] for i in notes["items"]]
    if dr.limit_met(cov["measured"], str(cov["op"]), cov["limit"]):
        assert by and "prism_layer_coverage" not in open_keys
        assert notes["settled"][0]["axis_key"] == "prism_layer_coverage"
        assert notes["settled"][0]["rule"] == "VER-7"
    else:
        assert not by and "prism_layer_coverage" in open_keys
    # the wake is judged from pictures: no measurement settles it, it stays a note
    assert "wake_resolution" in open_keys


def test_a_point_whose_measurement_misses_its_limit_stays_open(limits):
    outcome = fr.RunOutcome.from_graph_state(_state(mesh_manifest=_manifest(layer_coverage_pct=3.0)))
    assert fr.settled_by_measurement(outcome, "prism_layer_coverage") == ""
    keys = [i["axis_key"] for i in fr.reviewer_notes_caveat(outcome)["items"]]
    assert "prism_layer_coverage" in keys


def test_the_fea_face_accounting_settles_a_face_count_point(limits):
    # fb44fed7: "142 named Face_k groups while the CAD has 146 faces" - the 4 are MESH-8 skins
    q = {"fea_faces": {"cad": 146, "named": 142, "accounted": 4, "missing": 0, "complete": True}}
    outcome = fr.RunOutcome.from_graph_state(_state(mesh_manifest=_manifest(**q)))
    assert "accounted for" in fr.settled_by_measurement(outcome, "fea_faces_named")
    q["fea_faces"]["complete"] = False
    outcome = fr.RunOutcome.from_graph_state(_state(mesh_manifest=_manifest(**q)))
    assert fr.settled_by_measurement(outcome, "fea_faces_named") == ""


def test_an_axis_no_measurement_speaks_to_is_never_settled(limits):
    outcome = fr.RunOutcome.from_graph_state(_state())
    for axis in ("surface_capture", "wake_resolution", "farfield_clearance", "lumen_fidelity"):
        assert fr.settled_by_measurement(outcome, axis) == ""
