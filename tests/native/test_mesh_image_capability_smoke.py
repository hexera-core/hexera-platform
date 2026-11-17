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
    "snappy": ["blockMesh", "snappyHexMesh", "decomposePar", "reconstructParMesh",
               "surfaceFeatureExtract", "checkMesh"],
    "snappy_multiregion": ["blockMesh", "snappyHexMesh", "splitMeshRegions", "checkMesh"],
    # the snap-grid mesher writes its polyMesh itself; OpenFOAM only splits and checks it
    "snapgrid": ["splitMeshRegions", "checkMesh"],
    # Netgen meshes in its own interpreter (test below); OpenFOAM converts and checks the result
    "netgen": ["gmshToFoam", "checkMesh", "foamToVTK"],
}
_ALL_FOAM_TOOLS = sorted({t for tools in _FOAM_TOOLS.values() for t in tools})


def _foam(cmd: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-lc", f"source {BASHRC} >/dev/null 2>&1 && {cmd}"],
                          capture_output=True, text=True, timeout=timeout)


def test_dispatch_registry_resolves_every_supported_engine(canonical_provenance):
    expected = {"cfmesh", "snappy", "snappy_multiregion", "gmsh", "vmtk", "snapgrid", "netgen"}
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


def test_netgen_env_is_installed_and_imports():
    """The Netgen CFD engine's isolated interpreter (Dockerfile, requirements/netgen.txt): present,
    and it imports netgen's STL mesher and scipy (engines/netgen/netgen_mesh.py needs both)."""
    from meshpipeline.engines.netgen import runner as NG

    assert NG.available(), f"{NG.netgen_python()} is not in the mesh image"
    p = subprocess.run([NG.netgen_python(), "-c",
                        "import netgen.meshing, netgen.stl, scipy.spatial; from netgen import config; "
                        "print(config.version)"], capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr
    assert "6.2.2608" in p.stdout


def test_mmg3d_is_installed_and_improves_a_box_the_same_way_twice(tmp_path):
    """The FEA quality post-pass (MESH-mmg-1): the pinned mmg3d build is on PATH, links its libraries,
    keeps a box's surface exactly as it went in, and gives the same mesh twice (engines/fea_mmg.py)."""
    import hashlib

    import gmsh

    from meshpipeline.engines import fea_mmg as MM

    exe = MM.binary()
    assert exe, "mmg3d is not on PATH in the mesh image"
    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.occ.addBox(0, 0, 0, 0.05, 0.05, 0.05)
        gmsh.model.occ.synchronize()
        gmsh.option.setNumber("Mesh.MeshSizeMax", 0.01)
        gmsh.model.mesh.generate(3)
        ntags, xyz, tets, tris, refs = MM._volume_arrays(gmsh, 1)
    finally:
        gmsh.finalize()
    digests = []
    for k in range(2):
        work = tmp_path / f"run{k}"
        work.mkdir()
        out = MM.remesh(xyz, tris, refs, tets, exe=exe, timeout=120, workdir=work)
        assert out["ok"], out["why"]
        fig = MM.worst(out["xyz"], out["tets"])
        assert fig["invalid"] == 0
        digests.append(hashlib.sha256(out["xyz"].tobytes() + out["tets"].tobytes()).hexdigest())
    assert digests[0] == digests[1]
