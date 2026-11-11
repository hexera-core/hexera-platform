"""The Netgen CFD engine's own rules, without Netgen: the plan (NG-1..NG-5), the node order handed to
gmshToFoam, the naming of faces by the staged patch they lie on, the wall retype, and the case
contract reading of a staged boundary."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from meshpipeline.engines.netgen import netgen_mesh as NM
from meshpipeline.engines.netgen import plan as P


def _tube_field(r: float = 0.01, n: int = 400):
    """A straight tube of radius r: wall points, faces and the radius at each point."""
    th = np.linspace(0, 2 * np.pi, 20, endpoint=False)
    z = np.linspace(0, 0.2, n // 20)
    pts = np.array([[r * np.cos(a), r * np.sin(a), zz] for zz in z for a in th])
    faces = []
    for i in range(len(z) - 1):
        for j in range(20):
            a, b = i * 20 + j, i * 20 + (j + 1) % 20
            faces += [[a, b, a + 20], [b, b + 20, a + 20]]
    return pts, np.array(faces), np.full(len(pts), r)


def test_ng1_cells_across_the_local_passage():
    pts, faces, rad = _tube_field(0.01)
    plan = P.make_plan(wall_points=pts, wall_faces=faces, radius=rad, port_diameters=[0.02],
                       volume=np.pi * 0.01 ** 2 * 0.2, wall_area=2 * np.pi * 0.01 * 0.2,
                       walls=["wall"], layers=5, cell_limit=8_000_000)
    assert plan["cells_across"] == P.CELLS_ACROSS
    #
    assert plan["h_narrow_m"] == pytest.approx(0.02 / (P.CELLS_ACROSS * P.EDGE_PER_H))
    assert plan["maxh_m"] <= 0.02 / P.CEILING_DIVISIONS + 1e-12
    assert not plan["notes"]


def test_ng1_a_narrow_branch_gets_its_own_size():
    pts, faces, rad = _tube_field(0.01)
    rad = rad.copy()
    rad[: len(rad) // 4] = 0.002                      # a quarter of the wall bounds a 2 mm branch
    plan = P.make_plan(wall_points=pts, wall_faces=faces, radius=rad, port_diameters=[0.02, 0.004],
                       volume=1e-5, wall_area=0.0126, walls=["wall"], layers=5,
                       cell_limit=8_000_000)
    assert min(plan["sizes"]) == pytest.approx(0.004 / (P.CELLS_ACROSS * P.EDGE_PER_H))
    assert max(plan["sizes"]) == pytest.approx(0.02 / (P.CELLS_ACROSS * P.EDGE_PER_H))


def test_ng3_layer_stack_under_the_narrowest_cell():
    pts, faces, rad = _tube_field(0.01)
    plan = P.make_plan(wall_points=pts, wall_faces=faces, radius=rad, port_diameters=[0.02],
                       volume=6e-5, wall_area=0.0126, walls=["wall"], layers=5, cell_limit=8_000_000)
    stack = sum(plan["first_layer_m"] * P.GROWTH ** k for k in range(5))
    assert stack == pytest.approx(P.LAYER_STACK * plan["h_narrow_m"])
    assert [r["name"] for r in plan["rungs"]] == ["planned layers", "layers half as thick", "no layers"]
    assert plan["rungs"][1]["first_layer"] == pytest.approx(plan["first_layer_m"] / 2)


def test_ng3_no_layers_means_one_layer_free_rung():
    pts, faces, rad = _tube_field(0.01)
    plan = P.make_plan(wall_points=pts, wall_faces=faces, radius=rad, port_diameters=[0.02],
                       volume=6e-5, wall_area=0.0126, walls=["wall"], layers=0, cell_limit=8_000_000)
    assert [r["layers"] for r in plan["rungs"]] == [0]


def test_ng4_the_cell_limit_coarsens_toward_the_floor_and_says_so():
    pts, faces, rad = _tube_field(0.01)
    plan = P.make_plan(wall_points=pts, wall_faces=faces, radius=rad, port_diameters=[0.02],
                       volume=6e-5, wall_area=0.0126, walls=["wall"], layers=5, cell_limit=20_000)
    assert plan["cells_across"] >= P.CELLS_ACROSS_MIN
    assert plan["estimated_cells"] <= 20_000 * 1.01
    assert plan["notes"]


def test_write_plan_writes_a_bounded_size_file(tmp_path: Path):
    pts, faces, rad = _tube_field(0.01)
    plan = P.make_plan(wall_points=pts, wall_faces=faces, radius=rad, port_diameters=[0.02],
                       volume=6e-5, wall_area=0.0126, walls=["wall"], layers=5, cell_limit=8_000_000)
    out = P.write_plan(tmp_path, plan, pts)
    lines = (tmp_path / P.SIZE_FILE).read_text().splitlines()
    assert int(lines[0]) == len(pts) == out["size_points"]
    assert lines[-1] == "0"
    spec = json.loads((tmp_path / P.SPEC_FILE).read_text())
    assert "sizes" not in spec and spec["rungs"]
    assert "Netgen:" in P.plan_words(spec)


def test_closed_volume_of_a_cube():
    v = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], float)
    f = [[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5],
         [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]]
    assert P.closed_volume(v[np.array(f)]) == pytest.approx(1.0)


def test_netgen_elements_come_out_in_gmsh_order():
    pts = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [0, 1, 1]], float)
    tets, n = NM.oriented(np.array([[0, 2, 1, 3], [0, 1, 2, 3]]), pts, 4)
    assert n == 1
    for t in tets:
        a, b, c, d = pts[t]
        assert np.dot(np.cross(b - a, c - a), d - a) > 0
    prisms, n = NM.oriented(np.array([[0, 2, 1, 3, 5, 4]]), pts, 6)
    assert n == 1 and prisms[0].tolist() == [0, 1, 2, 3, 4, 5]


def test_point_triangle_distance():
    tri = np.array([[[0, 0, 0], [1, 0, 0], [0, 1, 0]]] * 3, float)
    p = np.array([[0.2, 0.2, 0.5], [2.0, 0.0, 0.0], [-1.0, -1.0, 0.0]])
    d = NM.point_triangle_distance(p, tri)
    assert d == pytest.approx([0.5, 1.0, np.sqrt(2.0)])


def test_read_solids_keeps_every_named_patch(tmp_path: Path):
    stl = tmp_path / "fb.stl"
    stl.write_text("solid wall\nfacet normal 0 0 0\nouter loop\nvertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\n"
                   "endloop\nendfacet\nendsolid wall\nsolid inlet\nfacet normal 0 0 0\nouter loop\n"
                   "vertex 0 0 1\nvertex 1 0 1\nvertex 0 1 1\nendloop\nendfacet\nendsolid inlet\n"
                   "solid wall\nfacet normal 0 0 0\nouter loop\nvertex 0 0 2\nvertex 1 0 2\nvertex 0 1 2\n"
                   "endloop\nendfacet\nendsolid wall\n")
    s = NM.read_solids(stl)
    assert sorted(s) == ["inlet", "wall"] and len(s["wall"]) == 2


_BOUNDARY = """FoamFile
{
    version     2.0;
    format      ascii;
    class       polyBoundaryMesh;
    object      boundary;
}
3
(
    wall
    {
        type            patch;
        physicalType    patch;
        nFaces          10;
        startFace       100;
    }
    inlet
    {
        type            patch;
        physicalType    patch;
        nFaces          2;
        startFace       110;
    }
    defaultFaces
    {
        type            empty;
        nFaces          0;
        startFace       112;
    }
)
"""


def test_walls_are_typed_wall_and_an_empty_default_patch_goes(tmp_path: Path):
    from meshpipeline.engines.netgen.deliverable import boundary_foam_types
    from meshpipeline.engines.netgen.runner import _drop_empty_default, retype_walls
    pm = tmp_path / "constant" / "polyMesh"
    pm.mkdir(parents=True)
    (pm / "boundary").write_text(_BOUNDARY)
    assert retype_walls(pm, ["wall"]) == ["wall"]
    _drop_empty_default(pm)
    assert boundary_foam_types(tmp_path) == {"wall": "wall", "inlet": "patch"}
    assert "physicalType" not in (pm / "boundary").read_text()
    assert "\n2\n(" in (pm / "boundary").read_text()


def test_case_contract_reads_the_staged_boundary(tmp_path: Path):
    from meshpipeline.engines import case_contract as cc
    (tmp_path / "fluid_boundary.stl").write_text(
        "solid pipe wall\nfacet normal 0 0 0\nouter loop\nvertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\n"
        "endloop\nendfacet\nendsolid pipe wall\nsolid in\nfacet normal 0 0 0\nouter loop\nvertex 0 0 1\n"
        "vertex 1 0 1\nvertex 0 1 1\nendloop\nendfacet\nendsolid in\n")
    (tmp_path / "netgen_staging.json").write_text(json.dumps({"wall_name": "pipe wall"}))
    (tmp_path / "patches_contract.txt").write_text(
        "  • pipe wall  →  type wall\n  • in  →  type inlet\n", encoding="utf-8")
    case = cc.read_case_boundary(tmp_path, "netgen")
    assert case is not None and case.patches == {"pipe wall": "wall", "in": "inlet"}


def test_netgen_is_registered_and_dispatched():
    from meshpipeline.engines.capability import ladder_order
    from meshpipeline.engines.dispatch import engine_runners
    from meshpipeline.engines.registry import get_spec
    spec = get_spec("netgen")
    assert "netgen" in engine_runners()
    assert "netgen" in ladder_order("internal") and "netgen" not in ladder_order("external")
    assert spec.build_driver is not None
    assert spec.delivered_mesh.prism_layers and spec.home_classes == ()
    assert [g.key for g in spec.gates][:2] == ["manifest_valid", "quality_floor"]


def test_samples_cover_a_long_sliver_end_to_end():
    # a CAD cylinder's sliver spans the pipe: its samples must reach the rim, not sit at its centre
    sliver = np.array([[[0, 0, 0], [1.0, 0, 0], [1.0, 0.01, 0]]])
    pts, owner = NM.sample_triangles(sliver, 0.05)
    assert owner.tolist() == [0] * len(pts)
    assert pts[:, 0].min() == pytest.approx(0.0) and pts[:, 0].max() == pytest.approx(1.0)
    gaps = np.diff(np.unique(np.round(pts[:, 0], 9)))
    assert gaps.max() <= 0.05 + 1e-9


def test_near_coincident_corners_are_welded_and_collapsed_triangles_dropped():
    a = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], float)
    b = np.array([[1, 0, 0], [1, 1, 0], [0, 1 + 1e-9, 0]], float)      # shares an edge, 1e-9 off
    sliver = np.array([[0, 0, 0], [1e-9, 0, 0], [0, 1, 0]], float)     # collapses when welded
    tris, labels, info = NM.welded(np.stack([a, b, sliver, a]), np.array([0, 1, 2, 0]))
    assert len(tris) == 2 and labels.tolist() == [0, 1]
    assert info["merged_corners"] >= 2 and info["dropped"] == 2
    assert np.array_equal(tris[1][2], tris[0][2])                       # one point now
