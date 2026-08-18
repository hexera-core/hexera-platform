# Responsibility: Verify the solvability gate uses established libraries and the uploader ships the OpenFOAM case.
from __future__ import annotations

from pathlib import Path

APP_DIR = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"


def test_solvability_gate_uses_established_libraries():
    # OpenFOAM-engine solvability is engine-owned (engines/cfmesh/, shared by
    # cfMesh + snappy): assemble the FV pressure-Poisson operator from the polyMesh
    # and solve it with PCG + algebraic multigrid (pyamg + scipy).
    src = (APP_DIR / "engines" / "cfmesh" / "solvability.py").read_text()
    assert "import pyamg" in src, "must use pyamg for the AMG preconditioner"
    assert "scipy" in src, "must use scipy for sparse linear algebra"
    assert "polyMesh" in src, "cfMesh solvability operates on the polyMesh"


def test_artifact_uploader_ships_openfoam_case_for_cfmesh():
    # The deliverable is ENGINE-DECLARED (spec.deliverable); the uploader is a
    # generic recipe executor with no per-engine branches or hardcoded names.
    from meshpipeline.engines.registry import get_spec
    for name in ("cfmesh", "snappy"):
        d = get_spec(name).deliverable
        assert d.bundle == "openfoam_case.tar.gz" and d.prefix == "openfoam_case"
        assert "constant/polyMesh" in d.required
        assert d.marker == "constant/polyMesh/owner"
    assert "system/meshDict" in get_spec("cfmesh").deliverable.required
    # snappy ships ITS recipe dicts (the shared bundle used to omit them)
    assert "system/snappyHexMeshDict" in get_spec("snappy").deliverable.required
    src = (APP_DIR / "application" / "artifact_uploader.py").read_text()
    assert "ArtifactType.mesh_bundle" in src
    assert "spec.deliverable" in src or "_recipe" in src
    for gone in ("mesh.cgns", "mesh.med", "Allmesh", "mesh_gen_script",
                 "openfoam_case.tar.gz", "gmsh_case.tar.gz"):
        assert gone not in src, f"uploader must not hardcode bundles: {gone}"
