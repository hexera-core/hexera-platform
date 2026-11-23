# Responsibility: Verify the v4 mesher panel's gmsh members against the real gmsh, inside a real
#                 WorkerPool: a box passes all three, a real ABC overlapping-facets file fails
#                 GmshDefault with its faces located, and one member's options never leak into the next.
# Boundaries: the native tier - gmsh and OCCT exist only here. The pool runs gmsh in worker processes;
#             this module touches OCP only to author the box fixture.
from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("gmsh", reason="gmsh is a native-tier dependency")
pytest.importorskip("OCP.STEPControl", reason="the CAD kernel is a native-tier dependency")

from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.gp import gp_Pnt
from OCP.IFSelect import IFSelect_RetDone
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

from meshpipeline.cad.repair_v4.verdict.gmsh_members import GmshAlt, GmshDefault, GmshProd
from meshpipeline.cad.repair_v4.verdict.panel import run_panel
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool

TIMEOUT_S = 600  # native runs are linux/amd64 emulated on arm64 and slow

_CORPUS = Path(os.environ.get("HEXERA_MESH_FAIL_CORPUS", "~/archductor/mesh-fail-corpus")).expanduser()


@pytest.fixture(scope="module")
def pool() -> Iterator[WorkerPool]:
    p = WorkerPool(1, start_timeout_s=180)
    try:
        yield p
    finally:
        p.close()


@pytest.fixture(scope="module")
def box_step(tmp_path_factory: pytest.TempPathFactory) -> str:
    path = tmp_path_factory.mktemp("panel") / "box.step"
    writer = STEPControl_Writer()
    writer.Transfer(BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 40.0, 30.0, 20.0).Shape(), STEPControl_AsIs)
    assert writer.Write(str(path)) == IFSelect_RetDone
    return str(path)


def test_box_passes_all_three(pool: WorkerPool, box_step: str) -> None:
    results = run_panel(pool, box_step, [GmshProd(), GmshDefault(), GmshAlt()], timeout_s=TIMEOUT_S)
    assert [r.name for r in results] == ["gmsh_prod", "gmsh_default", "gmsh_alt"]
    for r in results:
        assert r.status == "pass", r
        assert r.error_kind is None and r.located == []
        assert r.elements > 0
        assert r.min_sicn is not None and 0.01 <= r.min_sicn <= 1.0  # SICN is a 0..1 quality
        assert r.seconds > 0


@pytest.fixture(scope="module")
def slab_step(tmp_path_factory: pytest.TempPathFactory) -> str:
    """A 1000 x 1000 x 0.1 mm plate: it meshes, into slivers. Real gmsh gives it a minimum SICN of about
    0.001 to 0.002 on every profile (measured), a tenth of the way to the floor and well clear of it."""
    path = tmp_path_factory.mktemp("panel") / "slab.step"
    writer = STEPControl_Writer()
    writer.Transfer(BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 1000.0, 1000.0, 0.1).Shape(), STEPControl_AsIs)
    assert writer.Write(str(path)) == IFSelect_RetDone
    return str(path)


def test_a_real_sliver_mesh_is_rejected_by_the_sicn_floor(pool: WorkerPool, slab_step: str) -> None:
    """The floor, shown rejecting a real file: gmsh produces a tet mesh with no inverted element, and the
    only reason each member fails it is `min_sicn < 0.01`."""
    results = run_panel(pool, slab_step, [GmshProd(), GmshDefault(), GmshAlt()], timeout_s=TIMEOUT_S)
    for r in results:
        assert r.status == "fail" and r.error_kind == "low_quality", r
        assert r.elements > 0 and r.min_sicn is not None
        assert 0.0 < r.min_sicn < 0.01  # a tet mesh was made, none of it inverted, the worst is too thin


def test_installed_gmsh_defaults_are_the_ones_the_panel_assumes() -> None:
    """`GMSH_DEFAULT_ALGORITHM_*` stand in for options a profile leaves unset: read them from the gmsh here."""
    import gmsh

    from meshpipeline.cad.repair_v4.verdict import mesh_child

    gmsh.initialize()
    try:
        assert gmsh.option.getNumber("Mesh.Algorithm") == mesh_child.GMSH_DEFAULT_ALGORITHM_2D
        assert gmsh.option.getNumber("Mesh.Algorithm3D") == mesh_child.GMSH_DEFAULT_ALGORITHM_3D
    finally:
        gmsh.finalize()


@pytest.mark.parametrize("profile", ["prod", "default", "alt"])
def test_each_profile_really_meshes_with_its_algorithm_pair(profile: str) -> None:
    """Set a profile's options in real gmsh, read the two algorithm options back: they are `algorithms()`."""
    import gmsh

    from meshpipeline.cad.repair_v4.verdict import mesh_child

    gmsh.initialize()
    try:
        _scale, options = mesh_child._profile(profile, 1.0)
        for key, value in options.items():
            mesh_child._set(gmsh, key, value)
        assert (int(gmsh.option.getNumber("Mesh.Algorithm")), int(gmsh.option.getNumber("Mesh.Algorithm3D"))) == (
            mesh_child.algorithms(profile)
        )
    finally:
        gmsh.finalize()


def test_members_apply_their_own_sizes(pool: WorkerPool, box_step: str) -> None:
    """The options take effect: Alt meshes at a twentieth of the diagonal and Prod at a tenth, so Alt
    has more tets; and the three members do not all mesh the box the same way. (Element counts differ on
    size alone, so this cannot show algorithm diversity: `test_panel` pins the algorithm pairs.)"""
    prod, default, alt = run_panel(pool, box_step, [GmshProd(), GmshDefault(), GmshAlt()], timeout_s=TIMEOUT_S)
    assert alt.elements > prod.elements
    assert len({prod.elements, default.elements, alt.elements}) == 3


def test_options_do_not_leak_between_members(pool: WorkerPool, box_step: str) -> None:
    """The worker is warm and reused: GmshDefault after GmshProd must mesh as it does after nothing."""
    after_prod = run_panel(pool, box_step, [GmshProd(), GmshDefault()], timeout_s=TIMEOUT_S)[1]
    alone = run_panel(pool, box_step, [GmshDefault()], timeout_s=TIMEOUT_S)[0]
    assert after_prod.elements == alone.elements
    assert after_prod.min_sicn == alone.min_sicn


def test_abc_overlapping_facets_fails_default_with_located_faces(pool: WorkerPool) -> None:
    path = _CORPUS / "abc/00500400_bce216c87d2d17025db6ab36_step_000.step"
    if not path.is_file():
        pytest.skip(f"mesh-failure corpus file {path} is absent (set HEXERA_MESH_FAIL_CORPUS)")
    (result,) = run_panel(pool, str(path), [GmshDefault()], timeout_s=TIMEOUT_S)
    assert result.status == "fail", result
    assert result.error_kind == "overlapping_facets"
    assert result.located, "an overlapping-facets failure names the surfaces it is on"
    assert all(item.entity and item.entity.startswith("face:") for item in result.located)
    assert all(item.mesher_tag.startswith("surface ") for item in result.located)
    assert result.min_sicn is None and result.elements == 0


def test_located_entities_are_faces_of_the_file(pool: WorkerPool) -> None:
    """The fingerprints a member reports are the ones the workspace gives that file's own faces
    (`ws_index` over the same STEP read), so a later layer can map a failure to a stable face id."""
    path = _CORPUS / "abc/00500400_bce216c87d2d17025db6ab36_step_000.step"
    if not path.is_file():
        pytest.skip(f"mesh-failure corpus file {path} is absent (set HEXERA_MESH_FAIL_CORPUS)")
    (result,) = run_panel(pool, str(path), [GmshDefault()], timeout_s=TIMEOUT_S)
    with tempfile.TemporaryDirectory() as tmp:
        read = pool.call("read_step_xcaf", {"path": str(path), "brep_out": f"{tmp}/s.brep"}, timeout_s=TIMEOUT_S)
        assert read.ok and read.value is not None, read
        indexed = pool.call("ws_index", {"brep_path": read.value["brep"]}, timeout_s=TIMEOUT_S)
        assert indexed.ok and indexed.value is not None, indexed
    faces = {e["fp"] for e in indexed.value["entities"] if e["type"] == "face"}
    assert result.located
    assert {item.entity for item in result.located} <= faces
