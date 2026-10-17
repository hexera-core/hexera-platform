# Responsibility: Verify the repair node mints a review item for every defect it leaves to a person, and only then.
from __future__ import annotations

import asyncio

import pytest
from tests._geometry_support import geometry_state

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.conservative import RepairRefused
from meshpipeline.cad.repair.contracts import RepairMeasurement, RepairReport, RepairStatus
from meshpipeline.pipeline.repair_attempt import node_repair_attempt


def _entity(code, severity, kind, index, fingerprint) -> dict:
    return {"code": code, "severity": severity, "entity": f"{kind}:{index}",
            "entity_type": kind, "entity_index": index, "message": f"a {code}",
            "region": "unknown", "measurements": {"boundary_edges": 4}, "location": {},
            "fingerprint": fingerprint}


def _state(tmp_path, *entities) -> dict:
    return {"job_id": "job-1", "engine": "snappy", "geometry": geometry_state(tmp_path),
            "repair_status": RepairStatus.repairable.value,
            "repair_report": {"report": {
                "summary": "inspected",
                "defects": [{"code": e["code"], "severity": e["severity"]} for e in entities],
                "entities": list(entities)}}}


def _run(state) -> dict:
    return asyncio.run(node_repair_attempt(state))


def _op(name, status, code, target) -> dict:
    return {"name": name, "mutated": status == "kept", "status": status, "basis": "kernel",
            "targets": [target], "defect_code": code, "reason": "", "evidence": {}, "delta": {}}


def _report(*, operations=(), entities=(), **measured) -> RepairReport:
    return RepairReport(
        defects=(), operations=tuple(operations), entities=tuple(entities),
        measurements=tuple(RepairMeasurement(name=k, value=v) for k, v in measured.items()),
        summary="Repaired within the conservative caps.")


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENABLED", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_AUTONOMOUS", True)
    monkeypatch.setattr("meshpipeline.pipeline.repair_promote.stages_for",
                        lambda geometry, engine="": (True, ""))


def _small_and_open(tmp_path) -> dict:
    return _state(tmp_path, _entity("small_face", "warning", "face", 3, "fa"),
                  _entity("open_shell", "error", "edge", 8, "eb"))


def _scripted(monkeypatch, report=None, raises=None):
    calls = []

    def _repair(source, destination, **kw):
        calls.append(str(source))
        if raises:
            raise raises
        destination.write_bytes(source.read_bytes() + b"\n/* repaired */\n")
        return report

    monkeypatch.setattr("meshpipeline.cad.repair.conservative.repair_step_file", _repair)
    return calls


def test_with_review_off_a_small_face_still_sends_the_whole_part_to_manual_cleanup(
        tmp_path, monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REVIEW_ENABLED", False)
    calls = _scripted(monkeypatch)
    out = _run(_small_and_open(tmp_path))
    assert out["repair_attempt"]["attempted"] is False
    assert out["repair_attempt"]["route"] == "manual_cleanup"
    assert calls == []
    assert "repair_review_candidates" not in out


def test_with_review_on_repairables_run_and_judgement_defects_mint(tmp_path, monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REVIEW_ENABLED", True)
    calls = _scripted(monkeypatch, _report(
        operations=(_op("sew_faces", "kept", "open_shell", "eb"),),
        entities=(_entity("small_face", "warning", "face", 3, "fa"),),
        stopped_because="no_candidate", resolved=False, refusals=[], planner="rules-v1",
        operations_attempted=1, operations_kept=1))
    out = _run(_small_and_open(tmp_path))

    assert len(calls) == 1
    assert out["repair_attempt"]["attempted"] is True
    items = out["repair_review_candidates"]
    assert [(i["defect_code"], i["origin"]) for i in items] == [("small_face", "triage")]
    assert items[0]["group_key"] == "small_face:none"
    assert items[0]["pairing_key"] == "" and items[0]["proposal"] is None
    assert set(items[0]["camera"]) == {"centroid_mm", "normal", "extent_mm"}
    assert "a person decides" in items[0]["message"]


def test_review_on_with_judgement_only_does_not_decline_and_still_mints(tmp_path, monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REVIEW_ENABLED", True)
    calls = _scripted(monkeypatch, raises=RepairRefused(
        "nothing was kept", measurements={"operations": [], "stopped_because": "no_candidate",
                                          "refusals": [], "operations_kept": 0}))
    out = _run(_state(tmp_path, _entity("small_face", "warning", "face", 3, "fa")))
    assert len(calls) == 1
    assert [i["origin"] for i in out["repair_review_candidates"]] == ["triage"]


def test_a_stalled_run_mints_every_survivor_with_what_was_tried(tmp_path, monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REVIEW_ENABLED", True)
    tried = _op("sew_faces", "rolled_back", "open_shell", "eb")
    _scripted(monkeypatch, raises=RepairRefused(
        "nothing was kept", measurements={
            "operations": [tried], "stopped_because": "stalled", "refusals": [],
            "operations_attempted": 1, "operations_kept": 0}))
    out = _run(_state(tmp_path, _entity("open_shell", "error", "edge", 8, "eb"),
                      _entity("wire_gap", "error", "edge", 9, "ec")))
    items = {i["defect_code"]: i for i in out["repair_review_candidates"]}
    assert {i["origin"] for i in items.values()} == {"stall"}
    assert set(items) == {"open_shell", "wire_gap"}
    assert items["open_shell"]["measurements"]["tried"] == ["sew_faces"]
    assert "tried" not in items["wire_gap"]["measurements"]
    assert out["repair_attempt"]["attempted"] is False


def test_paired_rim_refusals_mint_with_a_pairing_key(tmp_path, monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REVIEW_ENABLED", True)
    rim_a = _entity("open_shell", "error", "edge", 1, "r1")
    rim_b = _entity("open_shell", "error", "edge", 2, "r2")
    refusals = [{"defect": "r1", "reason": "paired_rim", "detail": "tube"},
                {"defect": "r2", "reason": "paired_rim", "detail": "tube"}]
    _scripted(monkeypatch, _report(
        operations=(_op("sew_faces", "kept", "open_shell", "zz"),),
        entities=(rim_a, rim_b), stopped_because="no_candidate", resolved=False,
        refusals=refusals, planner="rules-v1", operations_attempted=1, operations_kept=1))
    out = _run(_state(tmp_path, rim_a, rim_b, _entity("wire_gap", "error", "edge", 9, "zz")))
    items = out["repair_review_candidates"]
    assert [i["origin"] for i in items] == ["refusal", "refusal"]
    assert {i["pairing_key"] for i in items} == {"r1,r2"}
    assert {i["fingerprint"] for i in items} == {"r1", "r2"}
    assert items[0]["measurements"]["refused_because"] == "paired_rim"


def test_review_off_mints_nothing_even_when_a_run_leaves_survivors(tmp_path, monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REVIEW_ENABLED", False)
    _scripted(monkeypatch, _report(
        operations=(_op("sew_faces", "kept", "open_shell", "eb"),),
        entities=(_entity("wire_gap", "error", "edge", 9, "ec"),),
        stopped_because="stalled", resolved=False, refusals=[], planner="rules-v1",
        operations_attempted=2, operations_kept=1))
    out = _run(_state(tmp_path, _entity("open_shell", "error", "edge", 8, "eb"),
                      _entity("wire_gap", "error", "edge", 9, "ec")))
    assert out["repair_review_candidates"] == []


@pytest.mark.parametrize("failure", [TimeoutError(), RuntimeError("boom")])
def test_a_timeout_or_crash_still_mints_what_a_person_decides(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REVIEW_ENABLED", True)
    _scripted(monkeypatch, raises=failure)
    out = _run(_small_and_open(tmp_path))
    assert out["repair_attempt"]["attempted"] is False
    assert [(i["defect_code"], i["origin"]) for i in out["repair_review_candidates"]] == [
        ("small_face", "triage")]


def test_a_repair_that_cannot_run_still_mints_triage_items(tmp_path, monkeypatch):
    from meshpipeline.cad.repair.conservative import RepairUnavailable
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REVIEW_ENABLED", True)
    _scripted(monkeypatch, raises=RepairUnavailable("no kernel"))
    out = _run(_small_and_open(tmp_path))
    assert [i["origin"] for i in out["repair_review_candidates"]] == ["triage"]


def test_the_node_reports_truncation_beside_the_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REVIEW_ENABLED", True)
    entities = [_entity("wire_gap", "error", "edge", i, f"w{i}") for i in range(1, 502)]
    _scripted(monkeypatch, raises=RepairRefused(
        "nothing was kept", measurements={"operations": [], "stopped_because": "no_candidate",
                                          "refusals": [], "operations_kept": 0}))
    out = _run(_state(tmp_path, *entities))
    assert len(out["repair_review_candidates"]) == 500
    assert out["repair_review_truncated"] == 1
