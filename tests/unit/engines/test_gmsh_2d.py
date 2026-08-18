# Responsibility: Verify a 2D Gmsh spec validates on curve tags, and planar structural work is admitted.
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

APP = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"
REPO_DIR = Path(__file__).parent.parent.parent.parent

from meshpipeline.engines.gmsh.driver import _validate_spec  # noqa: E402

_FIXTURE = REPO_DIR / "tests" / "fixtures" / "geometry" / "plate_with_hole_2d.step"

_SPEC_2D = {"element_order": 2, "dimensionality": "2D",
            "size": {"mode": "factor", "value": 0.03},
            "groups": [{"name": "fixed_left", "role": "fixed", "curve_tags": [4]},
                       {"name": "load_right", "role": "load", "curve_tags": [2]}],
            "default_group": "free", "optimize": True}


def test_2d_spec_validates_and_wrong_tag_fields_reject():
    assert _validate_spec(_SPEC_2D) == []
    # surface_tags is a 3D-only field
    bad = dict(_SPEC_2D, groups=[{"name": "a", "role": "fixed", "surface_tags": [1]}])
    errs = _validate_spec(bad)
    assert any("curve_tags" in e for e in errs), errs
    # and curve_tags is 2D-only
    bad3 = {"groups": [{"name": "a", "role": "fixed", "curve_tags": [1]}]}
    errs = _validate_spec(bad3)
    assert any("surface_tags" in e for e in errs), errs
    assert _validate_spec({"dimensionality": "4D"})


def test_gmsh_declares_2d_and_the_planar_capability():
    from meshpipeline.engines.registry import get_spec
    sp = get_spec("gmsh")
    assert "2D" in sp.input_contract.dimensionalities
    assert any(c.input_kind == "planar-domain" and c.output_kind == "surface-mesh"
               for c in sp.capabilities)


def test_admission_accepts_gmsh_planar_structural_2d():
    from meshpipeline.engines.admission import AdmissionEvidence, PatchSummary
    from meshpipeline.engines.registry import get_spec
    ev = AdmissionEvidence(
        engine="gmsh", purpose="structural", input_kind="planar-domain",
        dimensionality="2D",
        patches=(PatchSummary("fixed_left", "fixed"), PatchSummary("load_right", "load")),
        engine_params={"element_order": "2"}, surface_analysis=None)
    assert list(get_spec("gmsh").admit(ev)) == []


def test_committed_fixture_exists():
    assert _FIXTURE.exists(), f"the committed 2D fixture is missing: {_FIXTURE}"


@pytest.mark.skipif(not _FIXTURE.exists(), reason="fixture not generated")
def test_driver_meshes_the_planar_fixture_end_to_end(tmp_path):
    pytest.importorskip("gmsh")
    shutil.copy2(_FIXTURE, tmp_path / "geometry.step")
    (tmp_path / "dimensionality").write_text("2D")
    (tmp_path / "gmsh_spec.json").write_text(json.dumps(_SPEC_2D))
    proc = subprocess.run([sys.executable, "-m", "engines.gmsh.driver", str(tmp_path)],
                          cwd=str(APP), capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr[-800:]
    q = json.loads((tmp_path / "quality.json").read_text())
    assert q["dimensionality"] == "2D" and q["cells"] > 100
    assert q["min_sicn"] > 0.1
    # RECONCILIATION: groups read back from the meshed model, not echoed from the spec
    assert q["groups"] == {"fixed_left": "fixed", "load_right": "load", "free": "free"}
    # plane-stress elements in the deliverable deck
    assert "CPS6" in (tmp_path / "mesh.inp").read_text()


@pytest.mark.skipif(not _FIXTURE.exists(), reason="fixture not generated")
def test_driver_rejects_a_spec_that_contradicts_the_declared_dimensionality(tmp_path):
    pytest.importorskip("gmsh")
    shutil.copy2(_FIXTURE, tmp_path / "geometry.step")
    (tmp_path / "dimensionality").write_text("2D")
    (tmp_path / "gmsh_spec.json").write_text(json.dumps(dict(_SPEC_2D, dimensionality="3D",
        groups=[{"name": "fixed_left", "role": "fixed", "surface_tags": [1]}])))
    proc = subprocess.run([sys.executable, "-m", "engines.gmsh.driver", str(tmp_path)],
                          cwd=str(APP), capture_output=True, text=True, timeout=300)
    assert proc.returncode == 6
    assert "contradicts" in proc.stderr


@pytest.mark.skipif(not _FIXTURE.exists(), reason="fixture not generated")
def test_driver_rejects_unknown_curve_tags(tmp_path):
    pytest.importorskip("gmsh")
    shutil.copy2(_FIXTURE, tmp_path / "geometry.step")
    (tmp_path / "gmsh_spec.json").write_text(json.dumps(dict(
        _SPEC_2D, groups=[{"name": "fixed_left", "role": "fixed", "curve_tags": [99]}])))
    proc = subprocess.run([sys.executable, "-m", "engines.gmsh.driver", str(tmp_path)],
                          cwd=str(APP), capture_output=True, text=True, timeout=300)
    assert proc.returncode == 6
    assert "curve tags" in proc.stderr


@pytest.mark.external_fixture   # this ONE case needs the external elbow90 (3D); the rest use the committed 2D fixture
@pytest.mark.skipif(not _FIXTURE.exists(), reason="fixture not generated")
def test_driver_rejects_a_nonplanar_geometry_declared_2d(tmp_path):
    pytest.importorskip("gmsh")
    solid = REPO_DIR / "tests" / "fixtures" / "external" / "geometry" / "elbow90_fluid.step"
    if not solid.exists():
        pytest.skip("no 3D fixture on this machine")
    shutil.copy2(solid, tmp_path / "geometry.step")
    (tmp_path / "gmsh_spec.json").write_text(json.dumps(dict(_SPEC_2D, groups=[])))
    proc = subprocess.run([sys.executable, "-m", "engines.gmsh.driver", str(tmp_path)],
                          cwd=str(APP), capture_output=True, text=True, timeout=300)
    assert proc.returncode == 3
    assert "not planar" in proc.stderr
