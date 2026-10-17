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
