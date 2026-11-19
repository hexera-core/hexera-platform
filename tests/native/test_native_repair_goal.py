# Responsibility: Prove the goal profiles against the real kernel and the real gmsh probe - an
#                 unset goal changes nothing, and a standalone goal finishes with a tier-2 score.
# Boundaries: the native tier, because the finishing verdict's default prober writes a real BREP
#             and meshes it in a child interpreter. The verdict's arithmetic is unit-tested in
#             tests/unit/cad/test_repair_goal.py.
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from tests.cad_fixtures import write_loose_faces_step

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair import goal as goal_mod
from meshpipeline.cad.repair.conservative import repair_step_file
from meshpipeline.cad.repair.contracts import RepairGoal

pytest.importorskip("OCP.ShapeFix", reason="the CAD kernel is a native-tier dependency")


@pytest.fixture(autouse=True)
def _opted_in(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENABLED", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_DEVIATION_RATIO", 0.001)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_TOLERANCE_MM", 0.1)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ALLOW_FACE_REMOVAL", False)
    # THE ONE FIXTURE THIS LOOP ACTUALLY REPAIRS needs the same caps the report test gives it.
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_AREA_CHANGE_RATIO", 0.02)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO", 0.02)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_OPS", 6)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_STALL_LIMIT", 2)


def _measured(report) -> dict:
    return {m.name: m.value for m in report.measurements}


def _without_clocks(value):
    """The report as a dict, minus what a second run cannot reproduce: every measured second.

    The `timings` measurement is the obvious one; each operation also carries its own `apply_s`,
    `scan_s` and `measure_s`, which differ by microseconds between two identical runs. Everything
    else - the geometry, the verdicts, the declarations, the stop reason - must match exactly, and
    that is what this comparison is for.
    """
    if isinstance(value, dict):
        return {k: _without_clocks(v) for k, v in value.items()
                if not (k.endswith("_s") and k != "source_suffix") and k != "timings"}
    if isinstance(value, list):
        return [_without_clocks(v) for v in value]
    return value


def _comparable(report) -> dict:
    payload = _without_clocks(report.to_dict())
    payload["measurements"] = [m for m in payload["measurements"] if m["name"] != "timings"]
    return payload


def test_unset_goal_report_is_unchanged(tmp_path):
    """Default and `goal=None` are THE SAME RUN, and neither adds the key.

    This is the guarantee the whole task rests on: the flag-off path is byte-for-byte today's,
    including the driver's deadline (an unset goal reserves 0.0 seconds, so the expression the
    driver is handed is the one it was).
    """
    source = write_loose_faces_step(tmp_path / "loose.step")

    default = repair_step_file(source, tmp_path / "a.step")
    explicit = repair_step_file(source, tmp_path / "b.step", goal=None)

    assert "goal" not in _measured(default)
    assert "goal" not in _measured(explicit)
    assert _comparable(default) == _comparable(explicit)


def test_standalone_report_carries_the_verdict(tmp_path):
    pytest.importorskip("gmsh", reason="the tier-2 probe is a native-tier dependency")
    source = write_loose_faces_step(tmp_path / "loose.step")

    report = repair_step_file(source, tmp_path / "out.step", goal=RepairGoal.standalone)

    verdict = _measured(report)["goal"]
    assert verdict["profile"] == "standalone"
    assert "error" not in verdict, verdict
    assert isinstance(verdict["kernel_clean"], bool)
    meshability = verdict["meshability"]
    assert meshability["tier"] == "2"
    # A REAL ANSWER, not a skip: the run had time, so the probe ran and said something.
    assert meshability["skipped"] == ""
    assert isinstance(meshability["meshes"], bool)
    assert verdict["score"] in (0.0, 1.0)
    # AND IT STAYED INSIDE THE RESERVE IT TOOK, with the reaping slack the probe allows itself.
    assert meshability["seconds"] <= goal_mod.TIER2_HARD_CAP_S + 5.0
    assert meshability["budget_s"] <= goal_mod.TIER2_HARD_CAP_S


def test_meshing_goal_names_tier_three_without_probing(tmp_path):
    """A meshing job's verdict is the handback's name. The product mesher is not called here."""
    source = write_loose_faces_step(tmp_path / "loose.step")

    report = repair_step_file(source, tmp_path / "out.step", goal=RepairGoal.meshing)

    assert _measured(report)["goal"] == {
        "profile": "meshing", "final_verdict": "tier3_product_mesher"}


def test_the_attempt_node_passes_no_goal():
    """The product caller keeps today's behaviour until the task that gives it a goal.

    AN AST CHECK ON THE CALL, not a mock: a `goal=` kwarg appearing at this call site is exactly
    how a goal would reach every customer repair without anyone deciding it should.
    """
    import meshpipeline.pipeline.repair_attempt as node

    tree = ast.parse(Path(node.__file__).read_text())
    calls = [c for c in ast.walk(tree)
             if isinstance(c, ast.Call)
             and any(isinstance(a, ast.Name) and a.id == "repair_step_file" for a in c.args)]
    assert calls, "the to_thread(repair_step_file, ...) call site was not found"
    for call in calls:
        assert not [k for k in call.keywords if k.arg == "goal"], (
            "pipeline/repair_attempt.py passes a goal to repair_step_file")
