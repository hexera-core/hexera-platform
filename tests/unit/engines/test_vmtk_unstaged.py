# Responsibility: Verify VMTK says what stopped it when no lumen was prepared, and never starts the pype on nothing.
# Job a76e3ca1 (aortic arch, an STL): no lumen.vtp was staged, the builder spent ~30 turns on it, run_mesh still
# launched vmtk ("Error opening file lumen.vtp") and the run ended as a mesher crash. The faceted CAD aorta's
# staging failed ("not a watertight SOLID") and the run was refused as "this surface is closed" (2026-10-04).
from __future__ import annotations

import json

import meshpipeline.engines.vmtk.vmtk_runner as R


def test_configure_names_a_missing_lumen(tmp_path):
    out = R.configure_mesh(tmp_path, strategy={})
    assert out["code"] == "vmtk_lumen_not_staged" and "lumen.vtp is missing" in out["error"]


def _stage(tmp_path, monkeypatch, stage_lumen):
    """The builder's attempt seam, as a run reaches the engine's staging: a failure is recorded
    ONCE, as the attempt's pre-flight refusal (agents/builder/attempt), and the engine reads it."""
    import meshpipeline.engines.vmtk.lumen_staging as LS
    from meshpipeline.agents.builder import attempt
    monkeypatch.setattr(LS, "stage_lumen", stage_lumen)
    monkeypatch.setattr("meshpipeline.cad.staging.staged_surface",
                        lambda g, p: type("S", (), {"consumed": None})())
    geometry = type("G", (), {"path": str(tmp_path / "x.step")})()
    attempt._stage_declared(tmp_path, geometry, {"intake_patches": []}, "vmtk")


def test_a_staging_failure_is_recorded_and_named(tmp_path, monkeypatch):
    def _fail(*a, **kw):
        raise RuntimeError("internal-flow input is not a watertight SOLID")
    _stage(tmp_path, monkeypatch, _fail)
    assert "watertight SOLID" in R._staging_error(tmp_path)
    # even with the unopened CAD skin staged as lumen.vtp, the failure is what is reported
    (tmp_path / "lumen.vtp").write_text("not read")
    out = R.configure_mesh(tmp_path, strategy={})
    assert out["code"] == "vmtk_staging_failed" and "watertight SOLID" in out["error"]
    assert "watertight SOLID" in R.inspect_stl(tmp_path)["error"]


def test_a_successful_staging_clears_an_old_failure(tmp_path, monkeypatch):
    def _fail(*a, **kw):
        raise RuntimeError("old reason")
    _stage(tmp_path, monkeypatch, _fail)
    assert R._staging_error(tmp_path) == "old reason"
    _stage(tmp_path, monkeypatch, lambda *a, **kw: None)
    assert R._staging_error(tmp_path) == ""


def test_the_pype_is_never_started_without_a_lumen(tmp_path, monkeypatch):
    (tmp_path / "vmtk_spec.json").write_text(json.dumps({"edge_length_factor": 0.15}))
    started = []
    monkeypatch.setattr(R, "_run_pype", lambda *a, **kw: started.append(a) or {"rc": 0})
    res = R._run_vmtk_local(tmp_path, timeout=60)
    assert res["rc"] == 1 and "not started" in res["log_tail"] and not started
