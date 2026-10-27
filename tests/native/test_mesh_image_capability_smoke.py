# Responsibility: Verify every engine's executable or module is present, importable and versionable in the mesh image.
from __future__ import annotations

import shutil
import subprocess

import pytest

import meshpipeline.settings.runtime as rtcfg
from meshpipeline.engines.dispatch import engine_runners
from meshpipeline.engines.registry import engine_names, get_spec

pytestmark = pytest.mark.native_smoke

BASHRC = rtcfg.OPENFOAM_BASHRC

# engine → the OpenFOAM executables its native runner invokes. gmsh (python module) and vmtk
# (isolated conda shim) are handled by their own tests below.
_FOAM_TOOLS = {
    "cfmesh": ["cartesianMesh", "checkMesh"],
    # THE CONJUGATE MESHER ADDS topoSet, and it is the one tool no other engine needs. cfMesh
    # cannot mesh multiple regions, so this engine meshes ONE manifold domain and then cuts it:
    # topoSet tags the coolant cells against the coolant surface, splitMeshRegions cuts along
    # faces that already exist, and checkMesh runs PER REGION afterwards. A mesh image without
    # topoSet would fail the whole engine at its second stage.
    "cfmesh_conjugate": ["cartesianMesh", "topoSet", "splitMeshRegions", "checkMesh"],
    "snappy": ["blockMesh", "snappyHexMesh", "decomposePar", "reconstructParMesh",
               "surfaceFeatureExtract", "checkMesh"],
    "snappy_multiregion": ["blockMesh", "snappyHexMesh", "splitMeshRegions", "checkMesh"],
    # the snap-grid mesher writes its polyMesh itself; OpenFOAM only splits and checks it
    "snapgrid": ["splitMeshRegions", "checkMesh"],
}
_ALL_FOAM_TOOLS = sorted({t for tools in _FOAM_TOOLS.values() for t in tools})


def _foam(cmd: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-lc", f"source {BASHRC} >/dev/null 2>&1 && {cmd}"],
                          capture_output=True, text=True, timeout=timeout)


def test_dispatch_registry_resolves_every_supported_engine(canonical_provenance):
    expected = {"cfmesh", "cfmesh_conjugate", "snappy", "snappy_multiregion", "gmsh", "vmtk",
                "snapgrid"}
    assert set(engine_names()) == expected
    runners = engine_runners()
    assert set(runners) == expected
    for name in expected:
        assert callable(runners[name])
        assert get_spec(name).name == name
        assert get_spec(name).implemented is True


def test_openfoam_is_installed_and_versioned():
    r = _foam('printf %s "$WM_PROJECT_VERSION"')
    assert r.returncode == 0 and r.stdout.strip(), "OpenFOAM bashrc did not yield WM_PROJECT_VERSION"


@pytest.mark.parametrize("tool", _ALL_FOAM_TOOLS)
def test_openfoam_tool_present_and_help_queryable(tool):
    where = _foam(f"command -v {tool}")
    assert where.returncode == 0 and where.stdout.strip(), f"{tool} not on PATH in the mesh image"
    # OpenFOAM apps print a banner + accept -help; a bounded call proves the binary loads its libs.
    helped = _foam(f"{tool} -help", timeout=60)
    assert helped.returncode == 0, f"{tool} -help failed: {helped.stderr[-400:]}"


def test_gmsh_python_module_imports_and_initializes():
    import gmsh
    gmsh.initialize()
    try:
        version = gmsh.option.getString("General.Version")
    finally:
        gmsh.finalize()
    assert version and version[0].isdigit(), f"unexpected gmsh version {version!r}"


def test_vmtk_isolated_shim_present_and_env_healthy():
    exe = shutil.which(rtcfg.VMTK_BIN)
    assert exe, f"vmtk shim {rtcfg.VMTK_BIN!r} not on PATH in the mesh image"
    probe = subprocess.run(
        ["/opt/vmtk-env/bin/python", "-c",
         "import vtk,sys; print(vtk.vtkVersion.GetVTKVersion(), sys.version.split()[0])"],
        capture_output=True, text=True, timeout=60)
    assert probe.returncode == 0 and probe.stdout.strip(), f"vmtk conda env unhealthy: {probe.stderr[-400:]}"


def test_ftetwild_is_installed_and_meshes_a_box_the_same_way_twice(tmp_path):
    """FEA's last rung (LAD-fea-9): the pinned fTetWild build is on PATH, links its libraries, and its
    seeded build gives the same tetrahedra twice (engines/fea_tetwild.py)."""
    import hashlib

    import numpy as np

    from meshpipeline.engines import fea_tetwild as TW

    assert TW.binary(), "fTetWild is not on PATH in the mesh image"
    xyz = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]],
                   float) * 0.05
    tris = np.array([[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5],
                     [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]])
    digests = []
    for k in range(2):
        r = TW.run_part({"dir": str(tmp_path / f"run{k}"), "xyz": xyz, "tris": tris, "edge": 0.01,
                         "epsilon": 5e-5, "bg": None, "name": "box"})
        assert "error" not in r, r
        _t, vol = TW.orient(r["xyz"], r["tets"])
        assert (vol > 0).all() and vol.sum() == pytest.approx(0.05 ** 3, rel=1e-3)
        digests.append(hashlib.sha256(r["xyz"].tobytes() + r["tets"].tobytes()).hexdigest())
    assert digests[0] == digests[1]
