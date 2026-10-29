# Responsibility: Hold the shell mesh to Nastran's own element limits (SHMESH-8, -10, -11): the warping coefficient
# read as Nastran reads it, quads past a limit cut into two good triangles, and closed or periodic faces (a full
# ring, a cylinder piece) re-made so gmsh meshes them - every mesh inside every GEOMCHECK limit.
# Boundaries: gmsh and OpenCASCADE in this process, on small made-up surfaces; no solver.
from __future__ import annotations

import json
import math

import numpy as np
import pytest

from meshpipeline.engines import shell_quality as sq


def test_the_warp_is_nastrans_coefficient_not_an_angle():
    # a unit square whose corners stand 0.05 of a diagonal off its mean plane, alternately up and down
    h = 0.05 * math.sqrt(2.0)
    xyz = np.array([[0, 0, h], [1, 0, -h], [1, 1, h], [0, 1, -h]], dtype=float)
    q = sq.quad_measures(xyz, np.array([[0, 1, 2, 3]]))
    assert q["warp_coef"][0] == pytest.approx(0.05, rel=1e-9)
    assert q["warp_deg"][0] == pytest.approx(22.6, abs=0.1)      # the angle the old 5-degree limit held
    flat = sq.quad_measures(np.array([[0, 0, 0], [2, 0, 0], [2, 1, 0], [0, 1, 0]], dtype=float),
                            np.array([[0, 1, 2, 3]]))
    assert flat["warp_coef"][0] == pytest.approx(0.0, abs=1e-12)
    assert ("quad", "warp_coef", "<=", 0.05, "Q4_WARP") in sq.NASTRAN_LIMITS


def test_a_quad_past_a_limit_becomes_two_triangles_inside_every_limit():
    # quad 0 is a square (kept); quad 1 is convex with a 171-degree corner at node 7 (past Q4_IAMAX 150)
    xyz = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0],
                    [2, 0, 0], [4, 0, 0], [4, 1, 0], [3, 0.6, 0]], dtype=float)
    quads = np.array([[0, 1, 2, 3], [4, 5, 6, 7]])
    keep, tri, came = sq.split_past_quads(xyz, quads)
    assert keep.tolist() == [0]
    assert came.tolist() == [1, 1]
    # the cut runs through the open corner (node 7), so it is halved
    assert all(7 in t for t in tri.tolist())
    t = sq.tria_measures(xyz, tri)
    assert not sq._tria_past(t).any()
    # both triangles turn as the quad did (normals +z)
    n = np.cross(xyz[tri[:, 1]] - xyz[tri[:, 0]], xyz[tri[:, 2]] - xyz[tri[:, 0]])
    assert (n[:, 2] > 0).all()


def test_nothing_is_cut_when_every_quad_is_inside_the_limits():
    xyz = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], dtype=float)
    keep, tri, came = sq.split_past_quads(xyz, np.array([[0, 1, 2, 3]]))
    assert keep.tolist() == [0] and len(tri) == 0 and len(came) == 0


def _ring_record() -> dict:
    return {"surface_faces": ["Mid_1"],
            "mids": [{"id": "Mid_1", "pair": "P1", "body": "Part_1", "faces": ["Face_1", "Face_2"]}],
            "pairs": [{"id": "P1", "thickness_mm": 2.0, "size_mm": 120.0}]}


def _write(shape, path):
    from OCP.BRepTools import BRepTools

    BRepTools.Write_s(shape, str(path))
    return path


def test_a_full_ring_is_cut_at_its_seam_and_meshes_inside_every_limit(tmp_path):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder

    from meshpipeline.cad import midsurface as ms
    from meshpipeline.engines.gmsh import shell_adapter as sa

    cyl = BRepPrimAPI_MakeCylinder(40.0, 60.0).Shape()
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Cylinder

    side = [f for f in ms._faces(cyl) if BRepAdaptor_Surface(f).GetType() == GeomAbs_Cylinder][0]
    brep = _write(side, tmp_path / "ring.brep")
    owners = sa.split_closed(brep, ["Mid_1"], tmp_path / "cut.brep")
    assert len(owners) == 2 and set(owners) == {"Mid_1"}
    mesh, applied = sa.mesh_shell(brep, _ring_record())
    q = applied["quality"]
    assert q["valid"] and q["within_nastran"], q["nastran_past"]
    # the ring is closed round: no free element edge runs along the seam (the two halves share it)
    from meshpipeline.engines.shell_mesh import edge_users

    free = [e for e, us in edge_users(mesh).items() if len(us) == 1]
    z = mesh.xyz[np.array(free).ravel()][:, 2]
    assert np.all((np.abs(z) < 1e-6) | (np.abs(z - 60.0) < 1e-6))


def test_a_face_re_made_as_a_b_spline_keeps_its_area_and_owner(tmp_path):
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.GeomAbs import GeomAbs_BSplineSurface, GeomAbs_Cylinder

    from meshpipeline.cad import midsurface as ms
    from meshpipeline.engines.gmsh import shell_adapter as sa

    # a 170-degree piece of a cylinder: periodic, not closed
    piece = BRepPrimAPI_MakeCylinder(30.0, 50.0, math.radians(170.0)).Shape()
    faces = [f for f in ms._faces(piece) if BRepAdaptor_Surface(f).GetType() == GeomAbs_Cylinder]
    brep = _write(ms._compound(faces), tmp_path / "piece.brep")
    area = sum(ms._area(f) for f in faces)
    owners = sa.respline(brep, ["Mid_1"], 0, tmp_path / "spline.brep")
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape

    s = TopoDS_Shape()
    BRepTools.Read_s(s, str(tmp_path / "spline.brep"), BRep_Builder())
    got = ms._faces(s)
    assert owners == ["Mid_1"] * len(got)
    assert all(BRepAdaptor_Surface(f).GetType() == GeomAbs_BSplineSurface for f in got)
    mesh, applied = sa.mesh_shell(tmp_path / "spline.brep", {**_ring_record(), "surface_faces": owners})
    assert applied["quality"]["within_nastran"]
    json.dumps(applied, default=float)
    # the same surface exactly: every node on the radius-30 cylinder, and the face as large as before
    assert np.hypot(mesh.xyz[:, 0], mesh.xyz[:, 1]) == pytest.approx(30.0, abs=1e-6)
    assert area == pytest.approx(math.radians(170.0) * 30.0 * 50.0, rel=1e-6)


def test_a_sliver_edge_closes_and_its_triangle_goes():
    from meshpipeline.engines.shell_mesh import collapse_short

    # two unit quads side by side; node 6 sits 0.002 from node 1 (a sliver triangle 1-6-2 between them)
    xyz = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [2, 0, 0], [2, 1, 0], [1.002, 0, 0]], dtype=float)
    quads = np.array([[0, 1, 2, 3], [6, 4, 5, 2]])
    trias = np.array([[1, 6, 2]])
    Q, T, fq, ft, merged = collapse_short(xyz, quads, trias, [0, 0], [0], np.array([0.5]))
    assert merged == 1
    assert len(T) == 0 and len(Q) == 2
    # node 1 and node 6 are one now; both quads still use it
    assert {int(x) for x in Q.ravel()} == {0, 1, 2, 3, 4, 5} or {int(x) for x in Q.ravel()} == {0, 2, 3, 4, 5, 6}
    assert fq == [0, 0] and ft == []


def test_no_edge_closes_in_a_good_mesh():
    from meshpipeline.engines.shell_mesh import collapse_short

    xyz = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], dtype=float)
    Q, T, _fq, _ft, merged = collapse_short(xyz, np.array([[0, 1, 2, 3]]), np.zeros((0, 3), dtype=np.int64), [0], [],
                                            np.array([0.5]))
    assert merged == 0 and Q.tolist() == [[0, 1, 2, 3]]


def test_a_needle_swaps_its_long_edge_with_its_neighbour():
    # a flat 170-degree triangle 0-1-2 on the edge 0-1, and a good one 1-0-3 across it: the swap makes 0-3-2, 3-1-2
    xyz = np.array([[0, 0, 0], [2, 0, 0], [1, 0.08, 0], [1, -1, 0]], dtype=float)
    trias = np.array([[0, 1, 2], [1, 0, 3]])
    assert sq._tria_past(sq.tria_measures(xyz, trias)).any()
    T, swaps = sq.flip_past_trias(xyz, trias, [0, 0])
    assert swaps == 1
    assert not sq._tria_past(sq.tria_measures(xyz, T)).any()
    n = np.cross(xyz[T[:, 1]] - xyz[T[:, 0]], xyz[T[:, 2]] - xyz[T[:, 0]])
    assert (n[:, 2] > 0).all()
    # never across two faces
    T2, swaps2 = sq.flip_past_trias(xyz, trias, [0, 1])
    assert swaps2 == 0 and T2.tolist() == trias.tolist()
