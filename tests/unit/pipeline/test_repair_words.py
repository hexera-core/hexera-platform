# Responsibility: Verify the run's "Repair check" step says, in one plain line, what the repair check found and did - read only from what the repair nodes recorded.
# Boundaries: the wording and the two publications (lane, line); the repair itself is test_repair_attempt's.
from __future__ import annotations

import asyncio

import pytest
from tests._geometry_support import geometry_state
from tests.execution_publisher_double import install

import meshpipeline.application.execution_publisher as _ep
import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.contracts.narration import leaks
from meshpipeline.pipeline import repair_words as W
from meshpipeline.pipeline.repair_attempt import node_repair_attempt

_GAP = {"code": "wire_gap", "severity": "error", "entity": "edge:8", "region": "unknown",
        "message": "a gap", "measurements": {}, "location": {}}
_SELF = {"code": "self_intersection", "severity": "error", "entity": "face:3", "region": "unknown",
         "message": "faces cross", "measurements": {}, "location": {}}


def _report(*entities) -> dict:
    return {"status": "repairable" if entities else "clean",
            "report": {"summary": "inspected",
                       "defects": [{"code": e["code"], "severity": e["severity"], "count": 1}
                                   for e in entities],
                       "entities": [dict(e) for e in entities]}}


def _state(tmp_path, *entities, filename: str = "part.step", **over) -> dict:
    state = {"job_id": "job-1", "engine": "snappy",
             "geometry": geometry_state(tmp_path, filename=filename),
             "repair_status": "repairable" if entities else "clean",
             "repair_report": _report(*entities)}
    state.update(over)
    return state


def _op(name: str, code: str, *targets: str, status: str = "kept") -> dict:
    return {"name": name, "status": status, "defect_code": code, "targets": list(targets),
            "mutated": status == "kept", "reason": "", "basis": "kernel", "evidence": {}, "delta": {}}


# the five cases the step must tell apart


def test_a_clean_part_needed_no_repair(tmp_path):
    out = {"repair_attempt": {"attempted": False, "outcome": "triage", "route": "mesh_as_is"}}
    assert W.repair_check_words(_state(tmp_path), out) == "Your part needed no repair"
    # with repair switched off in the deployment, a clean part still needed none
    off = {"repair_attempt": {"attempted": False, "outcome": "off", "route": ""}}
    assert W.repair_check_words(_state(tmp_path), off) == "Your part needed no repair"


def test_a_repair_says_how_many_defects_and_what_was_done(tmp_path):
    out = {"repair_attempt": {"attempted": True, "operations": [
        _op("fix_face_boundary", "wire_gap", "edge:1"),
        _op("fix_face_boundary", "wire_gap", "edge:2"),
        _op("fix_shape_global", "bad_orientation", "face:9"),
        _op("sew_faces", "open_shell", "edge:4", status="rolled_back"),   # undone: not counted
    ]}}
    assert W.repair_check_words(_state(tmp_path, _GAP), out) == \
        "Repaired 3 defects: closed 2 gaps, flipped 1 face"


def test_a_repair_with_items_left_for_a_person_says_both(tmp_path):
    out = {"repair_attempt": {"attempted": True,
                              "operations": [_op("fix_face_boundary", "wire_gap", "edge:1")]},
           "repair_review_candidates": [{"item": 1}], "repair_review_truncated": 1}
    assert W.repair_check_words(_state(tmp_path, _GAP, _SELF), out) == \
        "Repaired 1 defect: closed 1 gap; 2 more need an engineer's look"


def test_a_repair_undone_for_changing_the_shape_meshes_the_original(tmp_path):
    out = {"repair_attempt": {"attempted": False, "outcome": "refused", "route": "conservative_repair",
                              "detail": {"measurements": {"result_discarded": True}}}}
    assert W.repair_check_words(_state(tmp_path, _GAP), out) == \
        "Repair undone: it changed the shape too much; meshing your original file"
    kept_nothing = {"repair_attempt": {"attempted": False, "outcome": "refused",
                                       "detail": {"measurements": {"operations_kept": 0}}}}
    assert W.repair_check_words(_state(tmp_path, _GAP), kept_nothing) == \
        "Nothing could be repaired safely; meshing your original file"


def test_defects_parked_for_review_ask_for_an_engineer(tmp_path):
    # triage sent it to a person; with review on, the items minted are what is counted
    out = {"repair_attempt": {"attempted": False, "outcome": "triage", "route": "manual_cleanup"},
           "repair_review_candidates": [{"item": 1}, {"item": 2}]}
    assert W.repair_check_words(_state(tmp_path, _SELF, _SELF), out) == \
        "2 defects need an engineer's look; meshing your original file"
    # with review off nothing is minted: the defects triage leaves to a person are counted
    bare = {"repair_attempt": {"attempted": False, "outcome": "triage", "route": "manual_cleanup"}}
    assert W.repair_check_words(_state(tmp_path, _SELF), bare) == \
        "1 defect needs an engineer's look; meshing your original file"


def test_an_iges_file_is_not_repaired_yet(tmp_path):
    out = {"repair_attempt": {"attempted": False, "outcome": "format"}}
    assert W.repair_check_words(_state(tmp_path, _GAP, filename="part.igs"), out) == \
        "IGES files are not repaired yet; meshing your original file"


# the edges


def test_nothing_is_said_when_there_was_nothing_to_check(tmp_path):
    out = {"repair_attempt": {"attempted": False, "outcome": "no_report"}}
    assert W.repair_check_words({"job_id": "j"}, out) is None


def test_an_inspection_that_did_not_finish_never_blames_the_file(tmp_path):
    st = _state(tmp_path, repair_status="inconclusive",
                repair_report={"service_failure": "inspection_timed_out"})
    out = {"repair_attempt": {"attempted": False, "outcome": "triage", "route": "abstain"}}
    assert W.repair_check_words(st, out) == \
        "The repair check could not finish; meshing your original file"


def test_every_line_is_plain_words(tmp_path):
    cases = [
        ({"attempted": True, "operations": [_op("some_new_op", "wire_gap", "e:1"),
                                            _op("another_op", "new_code")]}, ()),
        ({"attempted": False, "outcome": "format"}, (_GAP,)),
        ({"attempted": False, "outcome": "off"}, (_GAP,)),
        ({"attempted": False, "outcome": "timeout"}, (_GAP,)),
        ({"attempted": False, "outcome": "not_promoted"}, (_GAP,)),
        ({"attempted": False, "outcome": "triage", "route": "ask_customer"}, (_GAP,)),
        ({"attempted": False, "outcome": "triage", "route": "abstain"}, (_GAP,)),
    ]
    for record, entities in cases:
        line = W.repair_check_words(_state(tmp_path, *entities), {"repair_attempt": record})
        assert line and not leaks(line), (record, line)
        # one short line: the row it sits on ends in an ellipsis past about 75 characters
        assert len(line) <= 75, line


# the stream


@pytest.fixture
def published(monkeypatch):
    return install(monkeypatch, _ep)


def test_the_node_says_its_line_on_the_repair_lane(tmp_path, monkeypatch, published):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENABLED", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_AUTONOMOUS", True)
    out = asyncio.run(node_repair_attempt(_state(tmp_path, _GAP, filename="part.iges")))
    assert out["repair_attempt"]["outcome"] == "format"
    calls = [c for p in published for c in p.calls]
    assert [(c["agent"], c["method"], c.get("text")) for c in calls] == [
        ("repair", "anote", "IGES files are not repaired yet; meshing your original file")]


def test_the_inspection_opens_the_lane(tmp_path, monkeypatch, published):
    from meshpipeline.pipeline.repair_inspect import node_repair_inspect
    monkeypatch.setattr(rcfg, "CAD_INSPECT_MAX_BYTES", 0)    # skip the kernel: the lane is the point
    asyncio.run(node_repair_inspect(_state(tmp_path)))
    calls = [c for p in published for c in p.calls]
    assert [(c["agent"], c["method"]) for c in calls] == [("repair", "astage")]


def test_the_repair_lane_sits_between_engine_selection_and_mesh_creation():
    import meshpipeline.events as E
    stages = list(E.STAGES)
    assert stages.index("engine_select") < stages.index("repair") < stages.index("builder")
    assert E.note("repair", "Your part needed no repair").wire()["stage"] == "repair"


# THE SAME WORD AS THE STAGE (demo dry run, 2 Nov: the stud punch's stage said "The geometry is clean",
# its run "Found 42 defects; repair is off here" - for 36 tiny edges and 6 sliver faces, warnings only)

_TINY = {"code": "small_edge", "severity": "warning", "entity": "edge:235", "region": "unknown",
         "message": "This edge is far shorter than the part's scale", "measurements": {}, "location": {}}
_SLIVER = {"code": "small_face", "severity": "warning", "entity": "face:135", "region": "unknown",
           "message": "This face is a sliver relative to the part", "measurements": {}, "location": {}}


def test_a_clean_part_with_only_small_details_is_called_clean_not_defective(tmp_path):
    details = ([dict(_TINY, entity=f"edge:{k}") for k in range(36)]
               + [dict(_SLIVER, entity=f"face:{k}") for k in range(6)])
    state = _state(tmp_path, *details, repair_status="clean")
    off = {"repair_attempt": {"attempted": False, "outcome": "off", "route": ""}}
    line = W.repair_check_words(state, off)
    assert "defect" not in line and line.startswith("Your part is clean and needed no repair")
    assert "36 tiny edges and 6 sliver faces" in line and line.endswith("meshing your original file")


def test_a_real_defect_is_still_said_as_one(tmp_path):
    state = _state(tmp_path, _GAP, _TINY, repair_status="repairable")
    off = {"repair_attempt": {"attempted": False, "outcome": "off", "route": ""}}
    assert W.repair_check_words(state, off) == \
        "Found 2 defects; repair is off here, so meshing your original file"
