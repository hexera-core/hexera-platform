# Responsibility: Verify a replayed kept operation is the first attempt of a re-run and leaves the part no worse than the undirected run.
from __future__ import annotations

import pytest
from tests.cad_fixtures import write_loose_faces_step

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.conservative import repair_step_file

pytest.importorskip("OCP.ShapeFix", reason="the CAD kernel is a native-tier dependency")


@pytest.fixture(autouse=True)
def _opted_in(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENABLED", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_DEVIATION_RATIO", 0.001)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_TOLERANCE_MM", 0.1)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ALLOW_FACE_REMOVAL", False)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_AREA_CHANGE_RATIO", 0.02)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO", 0.02)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_OPS", 6)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_STALL_LIMIT", 2)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_TIMEOUT_S", 120.0)


def test_replayed_sew_then_rules_leaves_nothing_behind(tmp_path):
    source = write_loose_faces_step(tmp_path / "loose.step")

    plain = repair_step_file(source, tmp_path / "plain.step")
    kept = [o for o in plain.operations if o["mutated"]]
    assert kept, "the undirected run kept nothing, so there is nothing to replay"
    first = kept[0]
    replay = {"op": first["name"], "targets": list(first["targets"]),
              "defect_code": first["defect_code"],
              "defect_fingerprint": first["targets"][0], "source": "replay", "item_id": ""}

    # FROM THE ORIGINAL: the source file is never modified, so the same path serves.
    directed = repair_step_file(source, tmp_path / "directed.step", directives=(replay,))

    assert directed.operations[0]["name"] == first["name"]
    measured = {m.name: m.value for m in directed.measurements}
    assert measured["planner"] == "directed-v1"
    assert measured["operations_kept"] >= 1
    assert len(directed.entities) == len(plain.entities)


def _paired_rim_case(tmp_path):
    from tests.cad_fixtures import write_missing_bore_wall_step

    from meshpipeline.cad.repair.brep import _read_shape
    from meshpipeline.cad.repair.localize import localize_scan
    from meshpipeline.cad.repair.plan import RunHistory, paired_rim_fingerprints

    shape = _read_shape(write_missing_bore_wall_step(tmp_path / "bore.step"))
    defects, _ = localize_scan(shape)
    rims = sorted(paired_rim_fingerprints(shape, defects))
    assert len(rims) >= 2
    return shape, defects, rims, RunHistory()


def _fill(rim, source):
    return {"op": "fill_planar_hole", "targets": [rim], "defect_code": "open_shell",
            "defect_fingerprint": rim, "source": source, "item_id": "x"}


def test_an_engineer_fill_on_a_paired_rim_is_proposed(monkeypatch, tmp_path):
    from meshpipeline.cad.repair.directives import DirectedPlanner
    from meshpipeline.cad.repair.plan import RulesPlanner

    shape, defects, rims, history = _paired_rim_case(tmp_path)
    candidate = DirectedPlanner([_fill(rims[0], "engineer")], RulesPlanner()).propose(
        defects, history, shape=shape)

    assert candidate is not None and candidate.op.name == "fill_planar_hole"
    assert candidate.defect_fingerprint == rims[0]


def test_a_replay_fill_on_a_paired_rim_is_skipped_and_history_is_untouched(tmp_path):
    from meshpipeline.cad.repair.directives import DirectedPlanner

    class _Nothing:
        def propose(self, *a, **k):
            return None

    shape, defects, rims, history = _paired_rim_case(tmp_path)
    planner = DirectedPlanner([_fill(rims[0], "replay")], _Nothing())
    assert planner.propose(defects, history, shape=shape) is None

    assert [u["reason"] for u in planner.unplayable] == ["fill_refused"]
    assert history.fill_refused_because == "" and history.refusals == []


def test_a_result_the_after_check_discards_carries_its_attempts_and_says_so(tmp_path, monkeypatch):
    # the after-check is the last word on the result: when it refuses, the attempts (with the
    # session's kept verdicts) still travel, marked discarded, so no directive reads as done
    import meshpipeline.cad.repair.caps as caps
    import meshpipeline.cad.repair.loop  # noqa: F401 - bound BEFORE the patch: the session's
    import meshpipeline.cad.repair.session  # noqa: F401 - own per-op check must stay real
    from meshpipeline.cad.repair.conservative import RepairRefused
    monkeypatch.setattr(caps, "sanity_refusal",
                        lambda original, final, **declared: "the part moved too far")
    source = write_loose_faces_step(tmp_path / "loose.step")
    with pytest.raises(RepairRefused) as refused:
        repair_step_file(source, tmp_path / "out.step")
    measured = refused.value.measurements
    assert measured["result_discarded"] is True
    assert any(op.get("mutated") for op in measured["operations"]), measured["operations"]
    assert not (tmp_path / "out.step").exists()
