# Responsibility: Verify VMTK says what stopped it when no lumen was prepared, and never starts the pype on nothing.
# Job a76e3ca1 (aortic arch, an STL): no lumen.vtp was staged, the builder spent ~30 turns on it, run_mesh still
# launched vmtk ("Error opening file lumen.vtp") and the run ended as a mesher crash. The faceted CAD aorta's
# staging failed ("not a watertight SOLID") and the run was refused as "this surface is closed" (2026-10-04).
from __future__ import annotations

import json

import pytest

import meshpipeline.engines.vmtk.vmtk_runner as R


def test_configure_names_a_missing_lumen(tmp_path):
    out = R.configure_mesh(tmp_path, strategy={})
    assert out["code"] == "vmtk_lumen_not_staged" and "lumen.vtp is missing" in out["error"]


def test_a_staging_failure_is_recorded_and_named(tmp_path, monkeypatch):
    import meshpipeline.engines.vmtk.lumen_staging as LS

    def _fail(*a, **kw):
        raise RuntimeError("internal-flow input is not a watertight SOLID")
    monkeypatch.setattr(LS, "stage_lumen", _fail)
    with pytest.raises(RuntimeError):
        R.stage_declared(tmp_path, geometry_path=tmp_path / "x.step", prepared=None,
                         intake_patches=[])
    assert "watertight SOLID" in json.loads((tmp_path / R.STAGING_ERROR).read_text())["error"]
    # even with the unopened CAD skin staged as lumen.vtp, the failure is what is reported
    (tmp_path / "lumen.vtp").write_text("not read")
    out = R.configure_mesh(tmp_path, strategy={})
    assert out["code"] == "vmtk_staging_failed" and "watertight SOLID" in out["error"]
    assert "watertight SOLID" in R.inspect_stl(tmp_path)["error"]


def test_a_successful_staging_clears_an_old_failure(tmp_path, monkeypatch):
    import meshpipeline.engines.vmtk.lumen_staging as LS
    (tmp_path / R.STAGING_ERROR).write_text(json.dumps({"error": "old"}))
    monkeypatch.setattr(LS, "stage_lumen", lambda *a, **kw: None)
    assert R.stage_declared(tmp_path, geometry_path=tmp_path / "x.stl", prepared=None,
                            intake_patches=[]) is None
    assert not (tmp_path / R.STAGING_ERROR).exists()


def test_the_pype_is_never_started_without_a_lumen(tmp_path, monkeypatch):
    (tmp_path / "vmtk_spec.json").write_text(json.dumps({"edge_length_factor": 0.15}))
    started = []
    monkeypatch.setattr(R, "_run_pype", lambda *a, **kw: started.append(a) or {"rc": 0})
    res = R._run_vmtk_local(tmp_path, timeout=60)
    assert res["rc"] == 1 and "not started" in res["log_tail"] and not started
