# Responsibility: Verify a Gmsh delivery reaches the viewer with per-face quality fields aligned
# with the triangles it draws - first- and second-order tets, and hexes drawn as quads.
# Boundaries: real gmsh meshes of a unit box, written the way the driver writes mesh.msh.
from __future__ import annotations

import base64

import numpy as np
import pytest

gmsh = pytest.importorskip("gmsh")

from meshpipeline.engines.gmsh.viewer_surface import read_msh, surface_patches  # noqa: E402


def _f32(b64: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(b64), dtype=np.float32)


def _box(ws, *, order: int = 1, hexes: bool = False) -> None:
    """A unit box with its six faces grouped inlet (x=0) / outlet (x=1) / wall, meshed with
    tets - or hexes through a transfinite grid - and written as mesh.msh."""
    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("box")
        gmsh.model.occ.addBox(0, 0, 0, 1, 1, 1)
        gmsh.model.occ.synchronize()
        groups: dict[str, list[int]] = {"inlet": [], "outlet": [], "wall": []}
        for dim, tag in gmsh.model.getEntities(2):
            x, _y, _z = gmsh.model.occ.getCenterOfMass(dim, tag)
            groups["inlet" if x < 0.01 else "outlet" if x > 0.99 else "wall"].append(tag)
        for name, tags in groups.items():
            gmsh.model.addPhysicalGroup(2, tags, name=name)
        gmsh.model.addPhysicalGroup(3, [1], name="solid")
        if hexes:
            gmsh.model.mesh.setTransfiniteAutomatic(recombine=True)
        else:
            gmsh.option.setNumber("Mesh.MeshSizeMax", 0.35)
        gmsh.option.setNumber("Mesh.ElementOrder", order)
        gmsh.model.mesh.generate(3)
        gmsh.write(str(ws / "mesh.msh"))
    finally:
        gmsh.finalize()


def _hook():
    from meshpipeline.engines.gmsh.spec import _viewer_surface
    return _viewer_surface()


def _assert_aligned(resp: dict) -> dict:
    qf = resp["quality_fields"]
    drawn = {p["name"]: p["tri_count"] for p in resp["patches"]}
    assert set(qf["patches"]) == set(drawn) == {"inlet", "outlet", "wall"}
    for name, n in drawn.items():
        assert qf["patches"][name]["count"] == n
        for field in ("non_ortho_b64", "skewness_b64", "aspect_ratio_b64"):
            assert len(_f32(qf["patches"][name][field])) == n
        assert np.isfinite(_f32(qf["patches"][name]["non_ortho_b64"])).all()
    assert qf["metrics"]["non_ortho"]["limit"] == 65.0
    return qf


def test_a_tet_box_is_drawn_with_its_quality_fields(tmp_path):
    _box(tmp_path)
    resp = _hook()(tmp_path, roles={"inlet": "inlet"}, units="m")
    assert resp["kind"] == "stl"
    qf = _assert_aligned(resp)
    assert 0.0 < qf["metrics"]["non_ortho"]["max"] < 90.0           # tets are never orthogonal
    mesh = read_msh(tmp_path)
    assert [b.shape[1] for b in mesh["cells"]] == [4]
    assert sum(len(b) for b in mesh["cells"]) > 0


def test_second_order_elements_read_by_their_corners(tmp_path):
    _box(tmp_path, order=2)
    first = read_msh(tmp_path)
    assert [b.shape[1] for b in first["cells"]] == [4]              # tet10 -> its four corners
    assert all(len(poly) == 3 for _n, polys in first["patches"] for poly in polys)
    _assert_aligned(_hook()(tmp_path, roles={}, units="m"))


def test_hexes_are_drawn_as_quads_and_read_twice(tmp_path):
    _box(tmp_path, hexes=True)
    mesh = read_msh(tmp_path)
    assert 8 in [b.shape[1] for b in mesh["cells"]]
    quads = sum(1 for _n, polys in mesh["patches"] for poly in polys if len(poly) == 4)
    assert quads > 0
    resp = _hook()(tmp_path, roles={}, units="m")
    qf = _assert_aligned(resp)
    # a unit grid of cubes is orthogonal everywhere (to the rounding of gmsh's node placement)
    assert qf["metrics"]["non_ortho"]["max"] == pytest.approx(0.0, abs=1e-3)
    # the drawn surface is the same set of triangles surface_patches always returned
    drawn = surface_patches(tmp_path)
    assert {n: len(t) for n, t in drawn.items()} == {p["name"]: p["tri_count"] for p in resp["patches"]}


def test_no_mesh_yet_means_no_surface_and_no_fields(tmp_path):
    assert read_msh(tmp_path) is None
    assert surface_patches(tmp_path) == {}
    assert _hook()(tmp_path, roles={}, units="m") is None
