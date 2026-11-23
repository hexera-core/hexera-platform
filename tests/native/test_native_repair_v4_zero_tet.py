# Responsibility: Prove, against real gmsh, that a STEP which meshes to ZERO tetrahedra (a lone face, no solid)
#                 is answered as a scored file failure (`fail`/`no_volume`) by the real child process.
# Boundaries: the native tier. The hermetic counterpart (verdict status and the scoreboard) is
#             tests/unit/cad/repair_v4/test_zero_tet_verdict.py.
from __future__ import annotations

import pytest

pytest.importorskip("gmsh", reason="gmsh is a native-tier dependency")
pytest.importorskip("OCP.STEPControl", reason="the CAD kernel is a native-tier dependency")

from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
from OCP.gp import gp_Pln
from OCP.IFSelect import IFSelect_RetDone
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

from meshpipeline.cad.repair_v4.verdict.gmsh_members import result_from_call
from meshpipeline.cad.repair_v4.verdict.mesh_kernel import mesh_gmsh
from meshpipeline.cad.repair_v4.worker_pool import CallResult


@pytest.fixture(scope="module")
def lone_face_step(tmp_path_factory: pytest.TempPathFactory) -> str:
    path = tmp_path_factory.mktemp("zero_tet") / "face.step"
    writer = STEPControl_Writer()
    writer.Transfer(BRepBuilderAPI_MakeFace(gp_Pln(), 0.0, 10.0, 0.0, 10.0).Shape(), STEPControl_AsIs)
    assert writer.Write(str(path)) == IFSelect_RetDone
    return str(path)


@pytest.mark.parametrize("profile", ["prod", "default", "alt"])
def test_a_real_mesh_with_no_tetrahedra_is_a_file_failure(lone_face_step: str, profile: str) -> None:
    value = mesh_gmsh({"step": lone_face_step, "profile": profile, "timeout_s": 300})  # real child process
    assert value["status"] == "fail" and value["error_kind"] == "no_volume", value
    assert value["elements"] == 0
    member = result_from_call("gmsh_" + profile, CallResult(True, value, None, "ok", 1.0))
    assert member.status == "fail" and member.error_kind == "no_volume"
