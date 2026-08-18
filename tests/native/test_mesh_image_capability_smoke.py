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
}
_ALL_FOAM_TOOLS = sorted({t for tools in _FOAM_TOOLS.values() for t in tools})


def _foam(cmd: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-lc", f"source {BASHRC} >/dev/null 2>&1 && {cmd}"],
                          capture_output=True, text=True, timeout=timeout)


def test_dispatch_registry_resolves_every_supported_engine(canonical_provenance):
    expected = {"cfmesh", "snappy", "snappy_multiregion", "gmsh", "vmtk"}
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
