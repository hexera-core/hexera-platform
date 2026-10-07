# Responsibility: Verify the run repairs its own geometry only on evidence, and that every refusal leaves the customer's file in place.
from __future__ import annotations

import asyncio

import pytest
from tests._geometry_support import geometry_state

import meshpipeline.settings.cad_repair as rcfg
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


def _repaired_report(**measured) -> RepairReport:
    return RepairReport(
        defects=(), measurements=tuple(RepairMeasurement(name=k, value=v)
                                       for k, v in measured.items()),
        operations=({"name": "fix_wireframe", "mutated": True},),
        summary="Repaired within the conservative caps.")


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
        return _repaired_report(holes_filled=[], holes_left=[])

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
    assert out["repair_attempt"]["operations"] == ["fix_wireframe"]
    # the run now meshes the repair, not the upload
    assert out["geometry"]["ref"]["sha256"] != before
    assert out["repair_lineage"]["original"]["sha256"] == before
    assert out["repair_status"] == RepairStatus.repaired.value


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
