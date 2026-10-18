# Responsibility: Prove, on the real kernel, that the repair node's live LLM posture plays the model's directive first and the caps judge it.
# Boundaries: the model is a scripted router at the seam; the inspection, the repair and the caps verdicts are real OpenCASCADE.
# Honest scope: no fixture both stalls the rules planner and is repaired by a model's single-operation
#               choice (every catalogue operation on every stalling fixture is rolled back), so this
#               does not claim the model rescues a stalled part. It pins the node-level contract.
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from tests._geometry_support import interpretation_ref, source_ref
from tests.cad_fixtures import write_loose_faces_step, write_open_shell_step

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.contracts import model_inference
from meshpipeline.contracts.geometry_source import MaterializedGeometry

pytest.importorskip("OCP.ShapeFix", reason="the CAD kernel is a native-tier dependency")


@pytest.fixture(autouse=True)
def _opted_in(monkeypatch):
    settings = {
        "CAD_REPAIR_ENABLED": True, "CAD_REPAIR_AUTONOMOUS": True,
        "CAD_REPAIR_REVIEW_ENABLED": True, "CAD_REPAIR_MAX_DEVIATION_RATIO": 0.001,
        "CAD_REPAIR_MAX_TOLERANCE_MM": 0.1, "CAD_REPAIR_ALLOW_FACE_REMOVAL": False,
        "CAD_REPAIR_MAX_AREA_CHANGE_RATIO": 0.02, "CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO": 0.02,
        "CAD_REPAIR_MAX_OPS": 6, "CAD_REPAIR_STALL_LIMIT": 2, "CAD_REPAIR_TIMEOUT_S": 120.0}
    for name, value in settings.items():
        monkeypatch.setattr(rcfg, name, value)
    monkeypatch.setattr("meshpipeline.pipeline.repair_promote.stages_for",
                        lambda geometry, engine="": (True, ""))
    yield
    model_inference.set_model_router(None)


@pytest.fixture
def rows(monkeypatch):
    seen = []

    class _Log:
        def __init__(self, job_id): ...
        def log(self, kind, payload=None, op_id="", **kw):
            seen.append((kind, op_id, payload))

    monkeypatch.setattr("meshpipeline.capture.logger.TrainingLogger", _Log)
    return seen


class _Router:
    def __init__(self, proposals):
        self.proposals, self.calls = proposals, 0

    async def call_repair_review_model(self, messages, job_id="", name="RepairReview"):
        self.calls += 1
        return SimpleNamespace(ok=True, assistant_text=json.dumps({"proposals": self.proposals}))


def _node_state(source):
    from meshpipeline.cad.repair.brep import inspect_brep_file

    report = inspect_brep_file(source)
    ref = source_ref(local_file=source, filename=source.name)
    geometry = MaterializedGeometry(
        ref=ref, interpretation=interpretation_ref(geometry_source_id=ref.source_id),
        local_path=source).to_state()
    return report, {"job_id": "job-native", "engine": "snappy", "geometry": geometry,
                    "repair_status": "repairable", "repair_report": {"report": report.to_dict()}}


def _run(state):
    from meshpipeline.pipeline.repair_attempt import node_repair_attempt
    return asyncio.run(node_repair_attempt(state))


def _proposal(op, fingerprint):
    return {"op": op, "defect_fingerprint": fingerprint, "targets": [fingerprint],
            "confidence": 0.8, "rationale": "r"}


def _ops_of(rows_):
    return [r[2] for r in rows_ if r[0] == "repair_attempt"][-1]


def test_rules_only_stalls_on_the_open_shell_and_live_plays_the_models_directive_first(
        tmp_path, monkeypatch, rows):
    report, state = _node_state(write_open_shell_step(tmp_path / "part.step"))
    fingerprint = report.entities[0]["fingerprint"]

    monkeypatch.setattr(rcfg, "CAD_REPAIR_LLM_PLANNER", "off")
    off = _run(state)
    off_ops = _ops_of(rows)["operations"]
    # RULES-ONLY: sew_faces then fill_planar_hole, both rolled back by the real caps; nothing kept
    assert [(o["name"], o["status"]) for o in off_ops] == [
        ("sew_faces", "rolled_back"), ("fill_planar_hole", "rolled_back")]
    assert off["repair_attempt"]["attempted"] is False
    rows.clear()

    router = _Router([_proposal("fix_shape_global", fingerprint)])
    model_inference.set_model_router(router)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_LLM_PLANNER", "live")
    live = _run(state)

    assert router.calls >= 1
    logged = _ops_of(rows)
    ops = logged["operations"]
    # LIVE: the model's directive is the FIRST operation, stamped, and the real caps rolled it back
    assert (ops[0]["name"], ops[0]["status"]) == ("fix_shape_global", "rolled_back")
    assert ops[0]["source"] == "llm-planner" and ops[0]["directive_id"] == "llm:0"
    assert ops[0]["planner_confidence"] == 0.8
    assert not any(o["mutated"] for o in ops)
    assert live["repair_attempt"]["attempted"] is False
    directive_rows = logged["directives"]
    assert directive_rows == [{"item_id": "", "source": "llm-planner", "directive_id": "llm:0",
                               "planner_confidence": 0.8, "outcome": "rolled_back",
                               "reason": directive_rows[0]["reason"]}]


def test_live_model_directive_that_the_caps_accept_is_kept_with_its_provenance(
        tmp_path, monkeypatch, rows):
    report, state = _node_state(write_loose_faces_step(tmp_path / "part.step"))
    fingerprint = report.entities[0]["fingerprint"]
    router = _Router([_proposal("sew_faces", fingerprint)])
    model_inference.set_model_router(router)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_LLM_PLANNER", "live")

    out = _run(state)

    ops = _ops_of(rows)["operations"]
    assert (ops[0]["name"], ops[0]["status"], ops[0]["mutated"]) == ("sew_faces", "kept", True)
    assert ops[0]["source"] == "llm-planner" and ops[0]["directive_id"] == "llm:0"
    assert ops[0]["planner_confidence"] == 0.8
    assert out["repair_attempt"]["attempted"] is True
    assert _ops_of(rows)["directives"][0]["outcome"] == "kept"
