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
    # THESE TESTS ARE ABOUT THE v3 CONSERVATIVE REPAIR (its loop, caps and report keys). Since the CRS v5 beta
    # (#540) `CAD_REPAIR_ENGINE` defaults to v5, so the engine they exercise is named here, not inherited.
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENGINE", "v3")
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


def test_the_attempt_node_passes_only_a_goal_it_computed():
    """The product caller's goal is READ FROM THE RUN, never written into the call site.

    THIS TEST USED TO SAY `no goal`, and that was right until the standalone repair job existed.
    It now pins the thing that actually matters: the one `goal=` kwarg at this call site must be a
    NAME the node computed (`pipeline/repair_goal.from_state`, which consults the posture), never
    a constant or a `RepairGoal.<member>` attribute. A literal there is exactly how a goal would
    reach every customer repair without anyone deciding it should, and it is the only form of that
    mistake an AST check can catch before a deploy.

    That the computed value is `None` with the posture off, and the declared goal only when it is
    exactly live, is proved behaviourally in
    tests/unit/pipeline/test_repair_goal_dispatch.py::test_goal_reaches_repair_step_file_only_when_live.
    """
    import meshpipeline.pipeline.repair_attempt as node

    tree = ast.parse(Path(node.__file__).read_text())
    calls = [c for c in ast.walk(tree)
             if isinstance(c, ast.Call)
             and ast.unparse(c.func).endswith("to_thread")
             and any(isinstance(a, ast.Name) and a.id == "repair_step_file" for a in c.args)]
    assert calls, "the to_thread(repair_step_file, ...) call site was not found"
    passed = [k for call in calls for k in call.keywords if k.arg == "goal"]
    assert len(passed) == len(calls), (
        "the goal must be passed explicitly at every repair_step_file call site - an implicit "
        "default is a second rule nobody reads")
    for keyword in passed:
        assert isinstance(keyword.value, ast.Name), (
            "pipeline/repair_attempt.py hard-codes a goal at the kernel call site "
            f"({ast.unparse(keyword.value)}) - it must pass the one it computed from the run")
