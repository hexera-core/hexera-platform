# Responsibility: Verify the run repairs its own geometry only on evidence, and that every refusal leaves the customer's file in place.
from __future__ import annotations

import asyncio

import pytest
from tests._geometry_support import geometry_state

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.conservative import RepairRefused
from meshpipeline.cad.repair.contracts import (
    RepairMeasurement,
    RepairReport,
    RepairStatus,
)
from meshpipeline.pipeline.repair_attempt import node_repair_attempt

# THE INVARIANT EVERY TEST HERE CHECKS. The worst outcome of an automatic repair must be the
# behaviour of not having tried: the run carries on meshing the file the customer sent. So each
# refusal path is tested for the same two things - it declines, and `geometry` is untouched.


def _state(tmp_path, **over) -> dict:
    report = {"report": {"summary": "inspected",
                         "defects": [{"code": "wire_gap", "severity": "error"}],
                         "entities": [{"code": "wire_gap", "severity": "error",
                                       "entity": "edge:8", "region": "unknown",
                                       "message": "a gap", "measurements": {},
                                       "location": {"centroid": [1.0, 2.0, 3.0]}}]}}
    state = {"job_id": "job-1", "engine": "snappy",
             "geometry": geometry_state(tmp_path),
             "repair_status": RepairStatus.repairable.value,
             "repair_report": report}
    state.update(over)
    return state


def _run(state) -> dict:
    return asyncio.run(node_repair_attempt(state))


def _repaired_report(*, operations=(), defects=(), entities=(), **measured) -> RepairReport:
    """The report a looped repair returns. `operations` is the ordered attempt list."""
    return RepairReport(
        defects=tuple(defects),
        measurements=tuple(RepairMeasurement(name=k, value=v) for k, v in measured.items()),
        operations=tuple(operations),
        summary="Repaired within the conservative caps.",
        entities=tuple(entities))


@pytest.fixture(autouse=True)
def _opted_in(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENABLED", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_AUTONOMOUS", True)


@pytest.fixture
def repairs(monkeypatch):
    """A repair that writes a plausibly different file and reports success."""
    calls = []

    def _repair(source, destination, **kw):
        calls.append((str(source), str(destination)))
        destination.write_bytes(source.read_bytes() + b"\n/* repaired */\n")
        return _repaired_report(
            operations=({"name": "sew_faces", "mutated": True, "status": "kept",
                         "basis": "kernel", "targets": ["edge:aaaa"],
                         "defect_code": "open_shell", "reason": "",
                         "evidence": {}, "delta": {}},),
            stopped_because="resolved", resolved=True, complete_scan=True, refusals=[],
            planner="rules-v1", operations_attempted=1, operations_kept=1,
            declared_area_mm2=0.0, declared_volume_mm3=0.0)

    monkeypatch.setattr("meshpipeline.cad.repair.conservative.repair_step_file", _repair)
    monkeypatch.setattr("meshpipeline.pipeline.repair_promote.stages_for",
                        lambda geometry, engine="": (True, ""))
    return calls


# IT REPAIRS WHEN THE EVIDENCE SUPPORTS IT


def test_a_located_defect_is_repaired_and_the_result_is_what_gets_meshed(tmp_path, repairs):
    state = _state(tmp_path)
    before = state["geometry"]["ref"]["sha256"]

    out = _run(state)

    assert out["repair_attempt"]["attempted"] is True
    assert [op["name"] for op in out["repair_attempt"]["operations"]] == ["sew_faces"]
    # the run now meshes the repair, not the upload
    assert out["geometry"]["ref"]["sha256"] != before
    assert out["repair_lineage"]["original"]["sha256"] == before
    # `repair_status` is the inspection's verdict on the file the CUSTOMER SENT and is left alone;
    # what the repair did, and whether it resolved anything, lives in the attempt record beside
    # the report that justified it.
    assert "repair_status" not in out
    assert out["repair_attempt"]["repair_status"] == RepairStatus.repaired.value
    assert out["repair_attempt"]["resolved"] is True


def test_the_repair_is_written_beside_the_materialised_original(tmp_path, repairs):
    _run(_state(tmp_path))
    source, destination = repairs[0]
    # the job's own workspace, so the file survives for the builder that stages it
    assert destination.endswith("-repaired.step")
    assert destination.rsplit("/", 1)[0] == source.rsplit("/", 1)[0]


def test_the_attempt_records_what_it_aimed_at(tmp_path, repairs):
    out = _run(_state(tmp_path))
    # a reviewer approving this reads the defect that justified it beside the operation that
    # answered it
    targets = out["repair_attempt"]["targets"]
    assert targets and targets[0]["entity"] == "edge:8"
    assert targets[0]["location"]["centroid"] == [1.0, 2.0, 3.0]


def test_the_derived_geometry_keeps_the_customers_unit(tmp_path, repairs):
    state = _state(tmp_path)
    out = _run(state)
    # repair closes gaps; it does not decide a millimetre part was metres all along
    assert out["geometry"]["interpretation"]["unit"] == \
        state["geometry"]["interpretation"]["unit"]


def test_the_derived_identity_is_deterministic_in_the_repaired_bytes(tmp_path, repairs):
    # ONE upload, repaired twice - which is what a resumed run or a retried attempt does. The
    # identity has to come out the same, or every lineage record written about it names a
    # different thing than the one before.
    state = _state(tmp_path)
    first = _run(state)
    second = _run(_state(tmp_path, geometry=state["geometry"]))

    assert first["geometry"]["ref"]["source_id"] == second["geometry"]["ref"]["source_id"]
    assert ":repaired:" in first["geometry"]["ref"]["source_id"]
    # and it is derived from the ORIGINAL's id, so the chain back to the upload is readable
    assert first["geometry"]["ref"]["source_id"].startswith(
        state["geometry"]["ref"]["source_id"])


# EVERY REFUSAL LEAVES THE CUSTOMER'S FILE IN PLACE


def test_an_unopted_deployment_does_not_touch_the_geometry(tmp_path, monkeypatch, repairs):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENABLED", False)
    state = _state(tmp_path)

    out = _run(state)

    assert out["repair_attempt"]["attempted"] is False
    assert "geometry" not in out
    assert repairs == [], "the kernel must not be reached when repair is switched off"


def test_autonomy_can_be_switched_off_leaving_the_report_for_an_operator(tmp_path, monkeypatch,
                                                                        repairs):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_AUTONOMOUS", False)
    out = _run(_state(tmp_path))
    assert out["repair_attempt"]["attempted"] is False
    assert "operator" in out["repair_attempt"]["reason"]
    assert repairs == []


def test_a_defect_triage_will_not_recommend_repairing_is_left_alone(tmp_path, repairs):
    # self-intersection is a question about intent, which no cap can answer
    state = _state(tmp_path)
    state["repair_report"]["report"]["defects"] = [{"code": "self_intersection",
                                                    "severity": "error"}]
    state["repair_report"]["report"]["entities"] = [{"code": "self_intersection",
                                                     "severity": "error", "entity": "face:4"}]

    out = _run(state)

    assert out["repair_attempt"]["attempted"] is False
    assert out["repair_attempt"]["route"] == "manual_cleanup"
    assert "geometry" not in out
    assert repairs == []


def test_an_abstention_is_not_treated_as_permission(tmp_path, repairs):
    state = _state(tmp_path)
    state["repair_report"] = {"service_failure": "RuntimeError: occt unavailable"}

    out = _run(state)

    assert out["repair_attempt"]["attempted"] is False
    assert out["repair_attempt"]["route"] == "abstain"
    assert repairs == []


def test_a_refused_repair_leaves_the_run_on_the_original(tmp_path, monkeypatch):
    from meshpipeline.cad.repair.conservative import RepairRefused

    def _refuse(source, destination, **kw):
        raise RepairRefused("the repair lost 1 solid(s)", measurements={"before": {"solids": 1}})

    monkeypatch.setattr("meshpipeline.cad.repair.conservative.repair_step_file", _refuse)
    state = _state(tmp_path)

    out = _run(state)

    assert out["repair_attempt"]["attempted"] is False
    assert "lost 1 solid" in out["repair_attempt"]["reason"]
    # the caps' evidence travels with the decline, for the operator who decides what to do instead
    assert out["repair_attempt"]["detail"]["measurements"]["before"]["solids"] == 1
    assert "geometry" not in out


def test_a_repair_that_cannot_run_here_is_never_a_verdict_on_the_file(tmp_path, monkeypatch):
    from meshpipeline.cad.repair.conservative import RepairUnavailable

    def _unavailable(source, destination, **kw):
        raise RepairUnavailable("the CAD kernel is not installed in this process")

    monkeypatch.setattr("meshpipeline.cad.repair.conservative.repair_step_file", _unavailable)

    out = _run(_state(tmp_path))

    assert out["repair_attempt"]["attempted"] is False
    assert "could not run here" in out["repair_attempt"]["reason"]
    assert "geometry" not in out


def test_a_repair_that_crashes_does_not_fail_the_run(tmp_path, monkeypatch):
    def _boom(source, destination, **kw):
        raise ValueError("something unexpected in the kernel")

    monkeypatch.setattr("meshpipeline.cad.repair.conservative.repair_step_file", _boom)

    out = _run(_state(tmp_path))

    # a repair that breaks is never the file's fault, and the gate still gets to judge the original
    assert out["repair_attempt"]["attempted"] is False
    assert "ValueError" in out["repair_attempt"]["reason"]
    assert "geometry" not in out


def test_geometry_that_does_not_stage_after_repair_is_not_promoted(tmp_path, monkeypatch, repairs):
    monkeypatch.setattr("meshpipeline.pipeline.repair_promote.stages_for",
                        lambda geometry, engine="": (False, "RuntimeError: empty shape"))

    out = _run(_state(tmp_path))

    # it repaired, but the mesher cannot use the result - so the repair is evidence, not geometry
    assert out["repair_attempt"]["attempted"] is False
    assert "not promoted" in out["repair_attempt"]["reason"]
    assert "geometry" not in out


def test_a_run_with_no_inspection_attempts_nothing(tmp_path, repairs):
    state = _state(tmp_path)
    state["repair_report"] = {}
    out = _run(state)
    assert out["repair_attempt"]["attempted"] is False
    assert repairs == []


def test_a_run_with_no_geometry_attempts_nothing(repairs):
    out = _run({"job_id": "job-1", "engine": "snappy",
                "repair_status": "repairable",
                "repair_report": {"report": {"defects": [{"code": "wire_gap",
                                                          "severity": "error"}]}}})
    assert out["repair_attempt"]["attempted"] is False
    assert repairs == []


def test_a_format_the_bounded_repair_cannot_rewrite_is_left_alone(tmp_path, repairs):
    state = _state(tmp_path, geometry=geometry_state(tmp_path, filename="part.stl"))
    out = _run(state)
    # an IGES or STL path needs its own executor; half-handling it here would hand the builder a
    # file with the wrong suffix
    assert out["repair_attempt"]["attempted"] is False
    assert ".stl" in out["repair_attempt"]["reason"]
    assert repairs == []


def test_the_node_never_refuses_a_run(tmp_path, monkeypatch):
    # it is not a gate: nothing it returns may end a run or exhaust the retry ladder
    def _refuse(source, destination, **kw):
        from meshpipeline.cad.repair.conservative import RepairRefused
        raise RepairRefused("no")

    monkeypatch.setattr("meshpipeline.cad.repair.conservative.repair_step_file", _refuse)
    out = _run(_state(tmp_path))
    for forbidden in ("geometry_unsuitable_reason", "executor_success", "retry_count",
                      "api_failure"):
        assert forbidden not in out


def test_a_repair_that_outlives_its_welcome_leaves_the_run_on_the_original(tmp_path, monkeypatch):
    import time

    import meshpipeline.settings.cad_repair as cfg

    monkeypatch.setattr(cfg, "CAD_REPAIR_TIMEOUT_S", 0.05)
    monkeypatch.setattr("meshpipeline.cad.repair.conservative.repair_step_file",
                        lambda *a, **k: time.sleep(5))

    out = _run(_state(tmp_path))

    assert out["repair_attempt"]["attempted"] is False
    assert "exceeded" in out["repair_attempt"]["reason"]
    assert "geometry" not in out


def test_an_unreadable_repair_output_does_not_fail_the_run(tmp_path, monkeypatch):
    def _repair(source, destination, **kw):
        # reports success but leaves nothing behind - the file is gone when we go to hash it
        return _repaired_report()

    monkeypatch.setattr("meshpipeline.cad.repair.conservative.repair_step_file", _repair)

    out = _run(_state(tmp_path))

    # hashing a missing file is our problem, not the customer's
    assert out["repair_attempt"]["attempted"] is False
    assert "could not be adopted" in out["repair_attempt"]["reason"]
    assert "geometry" not in out


# WHAT THE RUN RECORDS ABOUT A LOOPED REPAIR


def _looped(monkeypatch, *, operations=(), entities=(), **measured):
    """Stub `repair_step_file` with a looped report, and stub staging so promotion succeeds."""
    def _repair(source, destination, **kw):
        destination.write_bytes(source.read_bytes() + b"\n/* repaired */\n")
        return _repaired_report(operations=operations, entities=entities, **measured)

    monkeypatch.setattr("meshpipeline.cad.repair.conservative.repair_step_file", _repair)
    monkeypatch.setattr("meshpipeline.pipeline.repair_promote.stages_for",
                        lambda geometry, engine="": (True, ""))


def test_the_run_records_every_operation_the_loop_tried(monkeypatch, tmp_path):
    # THE SLICE 8 CORPUS STARTS HERE. A rolled-back operation is as much a training example as a
    # kept one, so what is recorded is attempts, not successes.
    _looped(monkeypatch,
            operations=({"name": "fix_small_edges", "mutated": True, "status": "kept",
                         "basis": "kernel", "targets": ["edge:aaaa"],
                         "defect_code": "small_edge", "reason": "",
                         "evidence": {}, "delta": {}},
                        {"name": "fill_planar_hole", "mutated": False, "status": "rolled_back",
                         "basis": "kernel", "targets": ["edge:bbbb"],
                         "defect_code": "open_shell", "reason": "target_remains",
                         "evidence": {}, "delta": {}}),
            stopped_because="stalled", resolved=False, complete_scan=True, refusals=[],
            planner="rules-v1", operations_attempted=2, operations_kept=1,
            declared_area_mm2=0.0, declared_volume_mm3=0.0)

    attempt = _run(_state(tmp_path))["repair_attempt"]

    assert [op["name"] for op in attempt["operations"]] == \
           ["fix_small_edges", "fill_planar_hole"]
    assert attempt["operations"][1]["status"] == "rolled_back"
    assert attempt["operations"][1]["reason"] == "target_remains"
    assert attempt["stopped_because"] == "stalled"


def test_resolved_is_taken_from_the_loop_not_inferred(monkeypatch, tmp_path):
    # THE LIVE BUG THIS TASK FIXES. It used to be inferred from `holes_left`, a key the report no
    # longer carries - so `resolved` read True for any run with no warning defect, including a
    # run the loop explicitly reported as a failure.
    _looped(monkeypatch, stopped_because="stalled", resolved=False, complete_scan=True,
            refusals=[], planner="rules-v1", operations_attempted=2, operations_kept=0,
            declared_area_mm2=0.0, declared_volume_mm3=0.0,
            entities=({"code": "small_edge", "severity": "warning", "entity": "edge:9"},))

    attempt = _run(_state(tmp_path))["repair_attempt"]

    assert attempt["resolved"] is False, "the node contradicted the loop"
    assert attempt["defects_remaining"], "what is left was not recorded"


def test_a_run_that_stopped_on_a_partial_scan_is_not_recorded_as_resolved(
        monkeypatch, tmp_path):
    _looped(monkeypatch, stopped_because="scan_incomplete", resolved=False, complete_scan=False,
            refusals=[], planner="rules-v1", operations_attempted=0, operations_kept=0,
            declared_area_mm2=0.0, declared_volume_mm3=0.0)

    attempt = _run(_state(tmp_path))["repair_attempt"]

    assert attempt["resolved"] is False
    assert attempt["stopped_because"] == "scan_incomplete"


def test_a_planner_refusal_reaches_the_run_so_an_operator_can_read_it(monkeypatch, tmp_path):
    # The paired-rim case. A silent skip is how "we chose not to seal your bolt hole" stops being
    # something anybody can see.
    _looped(monkeypatch, stopped_because="no_candidate", resolved=False, complete_scan=True,
            refusals=[{"defect": "edge:rim", "reason": "paired_rim",
                       "detail": "would seal a through-hole"}],
            planner="rules-v1", operations_attempted=0, operations_kept=0,
            declared_area_mm2=0.0, declared_volume_mm3=0.0)

    refusals = _run(_state(tmp_path))["repair_attempt"]["refusals"]

    assert refusals[0]["reason"] == "paired_rim"
    assert refusals[0]["detail"] == "would seal a through-hole"


def test_the_inspections_verdict_on_the_customers_file_is_still_not_overwritten(
        monkeypatch, tmp_path):
    # `repair_status` is the inspection's verdict on the file the customer SENT, and
    # `repair_report` beside it is that inspection. Overwriting it with the repair's own verdict
    # produced a pairing that read "repaired" next to the original defect list.
    _looped(monkeypatch, stopped_because="resolved", resolved=True, complete_scan=True,
            refusals=[], planner="rules-v1", operations_attempted=1, operations_kept=1,
            declared_area_mm2=0.0, declared_volume_mm3=0.0)

    assert "repair_status" not in _run(_state(tmp_path))


def test_a_report_that_omits_resolved_is_not_recorded_as_resolved(monkeypatch, tmp_path):
    # Silence must never read as success: if a future report shape drops the key, fail closed.
    _looped(monkeypatch, stopped_because="resolved")

    assert _run(_state(tmp_path))["repair_attempt"]["resolved"] is False


# A DECLINE IS STILL A TRAINING ROW


def test_a_refused_zero_kept_run_still_records_what_it_tried(monkeypatch, tmp_path):
    """THE NEGATIVE EXAMPLES ARE THE VALUABLE ONES, and a decline must not throw them away.

    A run that keeps nothing is now refused before the file is written, so the success path's
    training log is never reached. An attempted-and-rolled-back sequence is exactly the training
    signal slice 8 needs - more valuable than a success, because it says what did NOT work - so
    the refusal carries the attempt list and the decline path logs it.
    """
    from meshpipeline.cad.repair.conservative import RepairRefused

    attempts = [{"name": "sew_faces", "mutated": False, "status": "rolled_back",
                 "reason": "target_remains", "basis": "geometric",
                 "targets": ["edge:a"], "defect_code": "open_shell",
                 "evidence": {}, "delta": {}},
                {"name": "fill_planar_hole", "mutated": False, "status": "rolled_back",
                 "reason": "declined:too_large (span ratio 0.8165)", "basis": "",
                 "targets": ["edge:a"], "defect_code": "open_shell",
                 "evidence": {}, "delta": {}}]

    def _refuse(source, destination, **kw):
        raise RepairRefused("nothing was kept (2 operation(s) attempted...)",
                            measurements={"operations": attempts, "operations_attempted": 2,
                                          "operations_kept": 0, "stopped_because": "stalled",
                                          "refusals": [], "planner": "rules-v1"})

    logged: list[tuple[str, dict]] = []

    class _Logger:
        def __init__(self, job_id):
            self.job_id = job_id

        def log(self, event_type, payload, **kw):
            logged.append((event_type, payload))

    monkeypatch.setattr("meshpipeline.cad.repair.conservative.repair_step_file", _refuse)
    monkeypatch.setattr("meshpipeline.capture.logger.TrainingLogger", _Logger)

    out = _run(_state(tmp_path))

    # the decline itself is unchanged: the run carries on with the customer's own file
    assert out["repair_attempt"]["attempted"] is False
    assert "geometry" not in out

    assert logged, "a refused run left no training row at all"
    event, payload = logged[0]
    assert event == "repair_attempt"
    assert [op["name"] for op in payload["operations"]] == ["sew_faces", "fill_planar_hole"]
    assert all(op["mutated"] is False for op in payload["operations"])
    assert payload["stopped_because"] == "stalled"


def test_a_training_logger_that_breaks_cannot_break_a_refused_run(monkeypatch, tmp_path):
    # The corpus is never load-bearing for a run, on the decline path as on the success one.
    from meshpipeline.cad.repair.conservative import RepairRefused

    def _refuse(source, destination, **kw):
        raise RepairRefused("nothing was kept", measurements={"operations": []})

    class _Broken:
        def __init__(self, job_id):
            raise RuntimeError("the capture backend is down")

    monkeypatch.setattr("meshpipeline.cad.repair.conservative.repair_step_file", _refuse)
    monkeypatch.setattr("meshpipeline.capture.logger.TrainingLogger", _Broken)

    out = _run(_state(tmp_path))

    assert out["repair_attempt"]["attempted"] is False
    assert "geometry" not in out


def test_the_runs_repair_directives_reach_the_repair_step(monkeypatch, tmp_path, repairs):
    seen = {}

    def _repair(source, destination, **kw):
        seen.update(kw)
        raise RepairRefused("nothing kept", measurements={"operations": []})

    monkeypatch.setattr("meshpipeline.cad.repair.conservative.repair_step_file", _repair)
    directive = {"op": "sew_faces", "targets": ["edge:aaaa"], "defect_code": "open_shell",
                 "defect_fingerprint": "edge:aaaa", "source": "replay", "item_id": ""}
    _run(_state(tmp_path, repair_directives=[directive]))
    assert seen["directives"] == (directive,)
    seen.clear()
    _run(_state(tmp_path))
    assert seen["directives"] == ()
