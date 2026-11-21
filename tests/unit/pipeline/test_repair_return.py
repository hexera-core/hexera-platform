# Responsibility: Verify a repair attempt names the STEP it returns - a refusal's passthrough labelled as unchanged, never repaired - and the hints sidecar beside it, for delivery and for the mesh.
from __future__ import annotations

import asyncio
import hashlib
import json

import pytest
from tests._geometry_support import geometry_state

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.conservative import RepairRefused
from meshpipeline.cad.repair.contracts import RepairStatus
from meshpipeline.pipeline import repair_return
from meshpipeline.pipeline.repair_attempt import node_repair_attempt


def _state(tmp_path) -> dict:
    report = {"report": {"summary": "inspected",
                         "defects": [{"code": "wire_gap", "severity": "error"}],
                         "entities": [{"code": "wire_gap", "severity": "error",
                                       "entity": "edge:8", "region": "unknown",
                                       "message": "a gap", "measurements": {},
                                       "location": {"centroid": [1.0, 2.0, 3.0]}}]}}
    return {"job_id": "job-1", "engine": "gmsh", "geometry": geometry_state(tmp_path),
            "repair_status": RepairStatus.repairable.value, "repair_report": report}


@pytest.fixture(autouse=True)
def _opted_in(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENABLED", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_AUTONOMOUS", True)


# the node publishes its Repair check line through the ownership-checked port; stood in for here,
# as in test_repair_attempt.py
@pytest.fixture(autouse=True)
def _execution_publisher(monkeypatch):
    from tests.execution_publisher_double import install

    import meshpipeline.application.execution_publisher as _ep
    return install(monkeypatch, _ep)


def _sidecar_for(step, path) -> str:
    digest = hashlib.sha256(step.read_bytes()).hexdigest()
    path.write_text(json.dumps({"schema": "crs-mesh-hints/1", "step": {"sha256": digest},
                                "faces": []}))
    return str(path)


def _refuses_with_passthrough(monkeypatch, *, hints: bool):
    """What `repair_step_file` does with the pass live and nothing kept: the upload's bytes at the
    destination, labelled passthrough, the sidecar beside them - then the refusal, unchanged."""
    def _repair(source, destination, **kw):
        destination.write_bytes(source.read_bytes())
        measured = {"operations": [], "delivery_source": "passthrough", "hints": "none"}
        if hints:
            side = destination.with_name(destination.name + ".mesh-hints.json")
            measured["hints"] = _sidecar_for(destination, side)
        raise RepairRefused("nothing was kept", measurements=measured)

    monkeypatch.setattr("meshpipeline.cad.repair.conservative.repair_step_file", _repair)


def test_a_refusal_with_a_passthrough_names_the_unchanged_step_and_its_sidecar(tmp_path,
                                                                               monkeypatch):
    _refuses_with_passthrough(monkeypatch, hints=True)
    state = _state(tmp_path)
    before = dict(state["geometry"])

    out = asyncio.run(node_repair_attempt(state))

    record = out["repair_attempt"]
    # THE REFUSAL STANDS: declined, refused, and the run stays on the customer's file
    assert record["attempted"] is False and record["outcome"] == "refused"
    assert "geometry" not in out and "repair_lineage" not in out
    assert state["geometry"] == before
    # ...AND THE STEP IS STILL RETURNED, labelled for what it is
    returned = record["returned"]
    assert returned["source"] == repair_return.PASSTHROUGH
    assert returned["source"] != "repaired"
    assert returned["path"].endswith("-repaired.step")      # the destination, holding the upload
    assert returned["sha256"] == before["ref"]["sha256"]     # byte-identical to the upload
    assert returned["hints_path"].endswith(".mesh-hints.json")
    assert repair_return.hints_path(out) == returned["hints_path"]


def test_a_refusal_without_hints_returns_the_step_alone(tmp_path, monkeypatch):
    _refuses_with_passthrough(monkeypatch, hints=False)
    out = asyncio.run(node_repair_attempt(_state(tmp_path)))
    assert out["repair_attempt"]["returned"]["hints_path"] == ""
    assert repair_return.hints_path(out) == ""


def test_with_the_pass_off_a_refusal_returns_nothing_new(tmp_path, monkeypatch):
    """Off, `repair_step_file` writes no file and records no delivery source: the record is the
    one it always was, key for key."""
    def _repair(source, destination, **kw):
        raise RepairRefused("nothing was kept", measurements={"operations": []})

    monkeypatch.setattr("meshpipeline.cad.repair.conservative.repair_step_file", _repair)
    out = asyncio.run(node_repair_attempt(_state(tmp_path)))
    assert "returned" not in out["repair_attempt"]


def _clean_part(monkeypatch, *, live: bool):
    """Triage keeps the part away from the repair (`mesh_as_is`); the pass is live or not."""
    from meshpipeline.cad.repair import triage

    monkeypatch.setattr(triage, "recommend", lambda **kw: triage.Recommendation(
        route="mesh_as_is", confidence=1.0, reasons=("the inspection found no defects",)))
    monkeypatch.setattr("meshpipeline.cad.repair.region_loop.meshability_pass_on", lambda: live)
    called = []

    def _hints(step, defects=(), **kw):
        called.append(str(step))
        side = step.with_name(step.name + ".mesh-hints.json")
        _sidecar_for(step, side)
        return side

    monkeypatch.setattr("meshpipeline.cad.repair.mesh_hints.hints_for", _hints)
    monkeypatch.setattr(
        "meshpipeline.cad.repair.conservative.repair_step_file",
        lambda *a, **k: pytest.fail("triage declined: the repair must not run"))
    return called


def test_a_part_triage_keeps_from_repair_still_returns_its_step_with_the_pass_live(tmp_path,
                                                                                  monkeypatch):
    called = _clean_part(monkeypatch, live=True)
    state = _state(tmp_path)
    out = asyncio.run(node_repair_attempt(state))
    record = out["repair_attempt"]
    assert record["outcome"] == "triage" and record["attempted"] is False
    assert "geometry" not in out
    returned = record["returned"]
    assert returned["source"] == repair_return.PASSTHROUGH
    assert returned["sha256"] == state["geometry"]["ref"]["sha256"]
    assert returned["hints_path"] and called == [returned["path"]]


def test_with_the_pass_off_a_part_triage_keeps_from_repair_returns_nothing(tmp_path, monkeypatch):
    called = _clean_part(monkeypatch, live=False)
    out = asyncio.run(node_repair_attempt(_state(tmp_path)))
    assert "returned" not in out["repair_attempt"] and called == []


def test_a_passthrough_whose_copy_failed_names_nothing(tmp_path):
    assert repair_return.returned({"delivery_source": "none: OSError"}, tmp_path / "x.step") == {}
    assert repair_return.returned({"delivery_source": "passthrough"}, tmp_path / "gone.step") == {}


def test_a_sidecar_the_disk_does_not_hold_is_not_offered(tmp_path):
    step = tmp_path / "p.step"
    step.write_bytes(b"ISO-10303-21;")
    out = repair_return.returned({"delivery_source": "passthrough",
                                  "hints": str(tmp_path / "missing.json")}, step)
    assert out["hints_path"] == "" and out["source"] == "passthrough"
    assert repair_return.returned({"delivery_source": "passthrough", "hints": "failed: X"},
                                  step)["hints_path"] == ""
