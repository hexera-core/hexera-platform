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
    owners, made = sa.respline(brep, ["Mid_1"], 0, tmp_path / "spline.brep")
    assert made == 1
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


def test_two_triangles_back_to_back_folded_flat_close_up():
    # SHMESH-12d: 0-1 a 2 mm edge, its far corners 2 and 3 0.09 mm apart (both triangles at 177 degrees): no edge is
    # short, a swap makes two other needles - the far corners merge and the pair goes; the outer quads close up
    from meshpipeline.engines import shell_mesh as sm

    xyz = np.array([[0, 0, 0], [2, 0, 0], [1, 0.04, 0], [1.09, -0.03, 0],
                    [0, 1, 0], [2, 1, 0], [0, -1, 0], [2, -1, 0]], dtype=float)
    trias = np.array([[0, 1, 2], [1, 0, 3], [2, 1, 5], [0, 2, 4], [3, 0, 6], [1, 3, 7]], dtype=np.int64)
    got = sm._folded_pairs(xyz, trias, [0] * 6, np.array([0.5]))
    assert [(round(d, 3), a, b, i, j) for d, a, b, i, j in got] == [(0.114, 2, 3, 0, 1)]
    _Q, T, _fq, _ft, merged = sm.collapse_short(xyz, np.zeros((0, 4), dtype=np.int64), trias, [], [0] * 6,
                                                np.array([0.5]))
    assert merged == 1 and sorted(map(sorted, T.tolist())) == [[0, 2, 4], [0, 2, 6], [1, 2, 5], [1, 2, 7]]
    # far corners a normal distance apart: nothing merges
    xyz2 = xyz.copy()
    xyz2[2, 1], xyz2[3, 1] = 0.8, -0.8
    assert sm._folded_pairs(xyz2, trias, [0] * 6, np.array([0.5])) == []


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


def test_a_swap_puts_no_triangle_past_an_outer_line_for_a_better_sum(monkeypatch):
    # SHMESH-89c: corners 28.5 / 17.4 degrees (one in the min-angle band, one past its 20-degree outer line); the swap
    # gives 14.6 and 141.1 (past the min outer line still, and now past the 140-degree max outer line) beside a good
    # triangle - a lower summed score, one more past an outer line
    lim = sq.InForce(aims=(("tria", "min_angle_tria", ">=", 30.0, 20.0, 0.01),
                           ("tria", "max_angle_tria", "<=", 120.0, 140.0, 0.01)))
    xyz = np.array([[0, 0, 0], [2, 0, 0], [0.25, 0.95, 0], [0.24, -0.55, 0]], dtype=float)
    trias = np.array([[0, 1, 2], [1, 0, 3]])
    T, swaps = sq.flip_past_trias(xyz, trias, [0, 0], limits=lim)
    assert swaps == 0 and T.tolist() == trias.tolist()
    monkeypatch.setattr(sq, "FLIP_SEVERE", False)
    T1, swaps1 = sq.flip_past_trias(xyz, trias, [0, 0], limits=lim, rounds=1)
    assert swaps1 == 1 and float(sq.tria_measures(xyz, T1, extra=True)["max_angle_tria"].max()) > 140.0


def _grid(n: int = 3) -> tuple[np.ndarray, np.ndarray]:
    """An n x n grid of unit quads in the plane z = 0 (node k at (k % (n+1), k // (n+1)))."""
    xyz = np.array([[i, j, 0.0] for j in range(n + 1) for i in range(n + 1)], dtype=float)
    quads = np.array([[j * (n + 1) + i, j * (n + 1) + i + 1, (j + 1) * (n + 1) + i + 1, (j + 1) * (n + 1) + i]
                      for j in range(n) for i in range(n)], dtype=np.int64)
    return xyz, quads


def test_an_inner_node_pulled_aside_moves_back_and_no_quad_is_left_past_a_limit():
    xyz, quads = _grid(2)                       # 4 quads round the inner node 4 at (1, 1)
    xyz[4] = [1.75, 1.7, 0.0]                   # pulled towards a corner: its quads past the limits
    none = np.zeros((0, 3), dtype=np.int64)
    assert sq._quad_past(sq.quad_measures(xyz, quads)).any()
    movable = np.zeros(len(xyz), dtype=bool)
    movable[4] = True
    moved, n = sq.relax_past(xyz, quads, none, movable)
    assert n >= 1
    assert not sq._quad_past(sq.quad_measures(moved, quads)).any()
    assert moved[4, 2] == 0.0                    # it stays in the plane of its face
    # a node on a CAD edge (not movable) never moves
    still, n0 = sq.relax_past(xyz, quads, none, np.zeros(len(xyz), dtype=bool))
    assert n0 == 0 and np.array_equal(still, xyz)


def test_relaxing_is_deterministic_and_leaves_a_good_mesh_alone():
    xyz, quads = _grid(3)
    none = np.zeros((0, 3), dtype=np.int64)
    movable = np.ones(len(xyz), dtype=bool)
    same, n = sq.relax_past(xyz, quads, none, movable)
    assert n == 0 and np.array_equal(same, xyz)
    xyz[5] = [1.8, 1.75, 0.0]
    a = sq.relax_past(xyz, quads, none, movable)
    b = sq.relax_past(xyz, quads, none, movable)
    assert a[1] == b[1] >= 1 and np.array_equal(a[0], b[0])


def test_a_small_hole_refines_round_itself_not_the_whole_plate(tmp_path):
    # a 500 x 300 x 2 plate with one 3 mm hole: the arcs of the hole are fine (24 round), the rim of the plate
    # keeps the size of the sheet - refining every edge of the face to the size of the hole made 60 times the elements
    from OCP.BRepTools import BRepTools
    from tests.fea_fixtures import _box, _cut, _cyl

    from meshpipeline.cad import midsurface as ms
    from meshpipeline.engines.gmsh import shell_adapter as sa

    shape = _cut(_box(0, 0, 0, 500, 300, 2.0), _cyl(100, 100, -1, 3.0, 4.0))
    rec, surface = ms.extract(shape, mm_per_unit=1.0)
    brep = tmp_path / "plate.brep"
    BRepTools.Write_s(surface, str(brep))
    mesh, applied = sa.mesh_shell(brep, rec)
    q = applied["quality"]
    assert q["valid"] and q["within_nastran"], q["nastran_past"]
    assert q["elements"] < 3000, q["elements"]
    # the element sides along the far rim (x = 500) are the size of the sheet, not of the hole
    ys = np.sort(mesh.xyz[mesh.xyz[:, 0] > 500 - 1e-6, 1])
    assert np.median(np.diff(ys)) > 10.0 * (3.0 * math.pi / 2 / 6), np.median(np.diff(ys))


def _cylinder_piece(tmp_path, degrees: float = 170.0, r: float = 30.0, h: float = 50.0):
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.GeomAbs import GeomAbs_Cylinder

    from meshpipeline.cad import midsurface as ms

    piece = BRepPrimAPI_MakeCylinder(r, h, math.radians(degrees)).Shape()
    faces = [f for f in ms._faces(piece) if BRepAdaptor_Surface(f).GetType() == GeomAbs_Cylinder]
    return _write(ms._compound(faces), tmp_path / "piece.brep")


def test_a_node_inside_a_curved_face_relaxes_on_its_surface():
    # SHMESH-14c: a 2 x 2 grid of quads wrapped on a radius-10 cylinder, its inner node pulled aside round the
    # axis; relaxed with the surface's own projection it moves back and stays on the cylinder
    ang = np.array([0.0, 0.1, 0.2])
    z = np.array([0.0, 1.0, 2.0])
    xyz = np.array([[10 * math.cos(a), 10 * math.sin(a), h] for h in z for a in ang])
    quads = np.array([[0, 1, 4, 3], [1, 2, 5, 4], [3, 4, 7, 6], [4, 5, 8, 7]])
    xyz[4] = [10 * math.cos(0.175), 10 * math.sin(0.175), 1.7]
    assert sq._quad_past(sq.quad_measures(xyz, quads)).any()
    movable = np.zeros(len(xyz), dtype=bool)
    movable[4] = True

    def onto(nodes, pts):
        r = np.hypot(pts[:, 0], pts[:, 1])[:, None]
        return np.column_stack([pts[:, :2] * (10.0 / r), pts[:, 2]])

    moved, n = sq.relax_past(xyz, quads, np.zeros((0, 3), dtype=np.int64), movable, project=onto)
    assert n >= 1
    assert not sq._quad_past(sq.quad_measures(moved, quads)).any()
    assert np.hypot(*moved[4, :2]) == pytest.approx(10.0, abs=1e-12)


def test_a_relaxed_cylinder_keeps_every_node_on_its_surface(tmp_path):
    from meshpipeline.engines.gmsh import shell_adapter as sa

    mesh, applied = sa.mesh_shell(_cylinder_piece(tmp_path), _ring_record())
    assert applied["quality"]["valid"] and applied["quality"]["within_nastran"]
    assert np.hypot(mesh.xyz[:, 0], mesh.xyz[:, 1]) == pytest.approx(30.0, abs=1e-6)


def test_a_face_whose_boundary_gmsh_cannot_recover_is_re_made_as_a_b_spline(tmp_path, monkeypatch):
    # SHMESH-11b: the first 2D mesh stops on the analytic cylinder ("impossible to recover the edge"); the face is
    # re-made as the same surface in B-spline form and the part meshed again
    import gmsh

    from meshpipeline.engines.gmsh import shell_adapter as sa

    real = gmsh.model.mesh.generate
    real_get = gmsh.logger.get
    calls = {"n": 0}

    def generate(dim=3):
        if dim == 2 and calls["n"] == 0:
            calls["n"] += 1
            raise Exception("Impossible to recover the edge 3 7")
        return real(dim)

    monkeypatch.setattr(gmsh.model.mesh, "generate", generate)
    monkeypatch.setattr(gmsh.logger, "get", lambda: real_get() + ["Info: Meshing surface 1 (Cylinder, Frontal-Delaunay)"])
    mesh, applied = sa.mesh_shell(_cylinder_piece(tmp_path), _ring_record())
    q = applied["quality"]
    assert q["resplined_faces"] == 1 and q["valid"] and q["within_nastran"]
    assert any("SHMESH-11" in n for n in mesh.notes)
    assert np.hypot(mesh.xyz[:, 0], mesh.xyz[:, 1]) == pytest.approx(30.0, abs=1e-6)


def test_a_face_whose_loop_gmsh_cannot_close_is_re_made_as_a_b_spline(tmp_path, monkeypatch):
    # SHMESH-11c: the first 2D mesh stops on the cylinder ("the 1D mesh seems not to be forming a closed loop"); the
    # face is re-made as a B-spline (no seam) and the part meshed again
    import gmsh

    from meshpipeline.engines.gmsh import shell_adapter as sa

    real = gmsh.model.mesh.generate
    real_get = gmsh.logger.get
    calls = {"n": 0}

    def generate(dim=3):
        if dim == 2 and calls["n"] == 0:
            calls["n"] += 1
            raise Exception("The 1D mesh seems not to be forming a closed loop")
        return real(dim)

    monkeypatch.setattr(gmsh.model.mesh, "generate", generate)
    monkeypatch.setattr(gmsh.logger, "get", lambda: real_get() + ["Info: Meshing surface 1 (Cylinder, Frontal-Delaunay)"])
    mesh, applied = sa.mesh_shell(_cylinder_piece(tmp_path), _ring_record())
    q = applied["quality"]
    assert q["resplined_faces"] == 1 and q["valid"]
    assert np.hypot(mesh.xyz[:, 0], mesh.xyz[:, 1]) == pytest.approx(30.0, abs=1e-6)


def test_a_b_spline_face_whose_loop_gmsh_cannot_close_is_not_re_made_again(tmp_path, monkeypatch):
    # SHMESH-11c: a face gmsh already reads as a B-spline is not re-made (it would fail the same way, again and again)
    import gmsh

    from meshpipeline.engines.gmsh import shell_adapter as sa

    real_get = gmsh.logger.get

    def generate(dim=3):
        raise Exception("The 1D mesh seems not to be forming a closed loop")

    monkeypatch.setattr(gmsh.model.mesh, "generate", generate)
    monkeypatch.setattr(gmsh.model, "getType", lambda dim, tag: "BSpline surface")
    monkeypatch.setattr(gmsh.logger, "get", lambda: real_get() + ["Info: Meshing surface 1 (BSpline surface, Frontal-Delaunay)"])
    with pytest.raises(Exception, match="closed loop"):
        sa.mesh_shell(_cylinder_piece(tmp_path), _ring_record())


@pytest.mark.parametrize(("ratio", "angle"), [(1.0, 90.0), (0.5, 45.0)])
def test_a_pipe_branch_meshes(tmp_path, ratio, angle, monkeypatch):
    # an equal tee and a 45 degree lateral (both failed: "1D mesh not a closed loop") mesh, every element valid
    from OCP.BRepTools import BRepTools
    from tests import shell_fixtures as sf

    from meshpipeline.cad import midsurface as ms
    from meshpipeline.engines.gmsh import shell_adapter as sa

    # its own stop: 5 to 16 s on a test VM and on the laptop, but under `ulimit -v 4194304` on the laptop (WSL) the
    # forked mesh never finished and held the run for 3 hours; stopped at 120 s it fails in words (SHMESH-21)
    monkeypatch.setattr(sa, "MESH_DEADLINE_S", 120.0)

    rec, surface = ms.extract(sf.pipe_branch(ratio=ratio, angle_deg=angle), mm_per_unit=1.0)
    brep = tmp_path / "branch.brep"
    BRepTools.Write_s(surface, str(brep))
    mesh, applied = sa.mesh_shell(brep, rec)
    assert applied["quality"]["valid"]
    assert applied["quality"]["tria_share"] < 0.05


def test_a_plan_past_the_element_cap_stops_before_gmsh_and_says_why(tmp_path, monkeypatch):
    # SHMESH-20: a plan asking for more elements than a shell mesh is made of in the time allowed fails fast, in words
    from meshpipeline.engines.gmsh import shell_adapter as sa

    monkeypatch.setattr(sa, "PLANNED_MAX", 10.0)
    with pytest.raises(ValueError, match="SHMESH-20"):
        sa.mesh_shell(_cylinder_piece(tmp_path), _ring_record())


def test_a_mesh_that_hangs_is_stopped_at_the_deadline_and_says_why(tmp_path, monkeypatch):
    # SHMESH-21: gmsh stuck in one 2D mesh (no way back to Python) is stopped by the clock, in words
    import time

    import gmsh

    from meshpipeline.engines.gmsh import shell_adapter as sa

    def generate(dim=3):
        time.sleep(30)

    monkeypatch.setattr(gmsh.model.mesh, "generate", generate)
    monkeypatch.setattr(sa, "MESH_DEADLINE_S", 2.0)
    started = time.monotonic()
    with pytest.raises(ValueError, match="SHMESH-21"):
        sa.mesh_shell(_cylinder_piece(tmp_path), _ring_record())
    assert time.monotonic() - started < 15.0


def test_the_record_tells_each_mid_face_its_triangles_and_elements_past_a_limit():
    """SHMESH-51o13: quality.per_face - per mid-face, its triangles and its elements past each limit, only where any
    (a run's totals said 47 triangles and 47 quads past max_angle_quad, never on which plate)."""
    from meshpipeline.engines.shell_mesh import ShellMesh

    xyz = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0],         # a unit square
                    [2, 0, 0], [3, 0, 0], [2.6, 0.2, 0], [2, 1, 0],     # a quad with a 153 degree corner
                    [4, 0, 0], [5, 0, 0], [4, 1, 0]], dtype=float)      # a right triangle
    mesh = ShellMesh(xyz=xyz, quads=np.array([[0, 1, 2, 3], [4, 5, 6, 7]]), trias=np.array([[8, 9, 10]]),
                     wall=np.ones(3), mid=["Mid_1", "Mid_2", "Mid_2"], part=["Part_1"] * 3)
    got = sq.per_face(mesh)
    assert "Mid_1" not in got
    assert got["Mid_2"]["trias"] == 1 and got["Mid_2"]["max_angle_quad"] == 1, got


def test_a_sharp_triangle_is_counted_against_the_users_angle_target():
    """SHMESH-51o34: a delivered triangle with a 24 degree corner counts against a 30 degree triangle target in
    per_face (as a deck reader sees it), though it is inside the industry's 20 degree line."""
    from meshpipeline.engines.shell_mesh import ShellMesh

    a = math.radians(24.0)
    xyz = np.array([[0, 0, 0], [10, 0, 0], [10 * math.cos(a), 10 * math.sin(a), 0],    # 24 / 78 / 78 degrees
                    [20, 0, 0], [30, 0, 0], [25, 8.66, 0]], dtype=float)                # 60 / 60 / 60
    mesh = ShellMesh(xyz=xyz, quads=np.zeros((0, 4), dtype=np.int64), trias=np.array([[0, 1, 2], [3, 4, 5]]),
                     wall=np.ones(2), mid=["Mid_1", "Mid_2"], part=["Part_1"] * 2)
    aims = (("tria", "min_angle_tria", ">=", 30.0, 20.0, 0.01),)
    got = sq.per_face(mesh, aims=aims)
    assert got["Mid_1"].get("min_angle_tria_outside_target") == 1, got
    assert "min_angle_tria_outside_target" not in got.get("Mid_2", {}), got
    assert "min_angle_tria" not in got["Mid_1"]           # (inside the industry's 20 degree line)


def test_the_record_tells_a_sharp_triangle_its_angles_and_why_its_nodes_stay():
    """SHMESH-51o34: the adapter's list of delivered triangles under the user's angle target - face, three angles,
    and per node why it may or may not move."""
    from meshpipeline.engines.gmsh import shell_adapter as SA
    from meshpipeline.engines.shell_mesh import ShellMesh

    a = math.radians(24.0)
    xyz = np.array([[0, 0, 0], [10, 0, 0], [10 * math.cos(a), 10 * math.sin(a), 0],
                    [20, 0, 0], [30, 0, 0], [25, 8.66, 0]], dtype=float)
    mesh = ShellMesh(xyz=xyz, quads=np.zeros((0, 4), dtype=np.int64), trias=np.array([[0, 1, 2], [3, 4, 5]]),
                     wall=np.ones(2), mid=["Mid_1", "Mid_2"], part=["Part_1"] * 2)
    lim = sq.InForce(aims=(("tria", "min_angle_tria", ">=", 30.0, 20.0, 0.01),))
    why = ["at a CAD vertex", "a band's or transition cell's node (pinned)", "free", "free", "free", "free"]
    told = SA._trias_told(mesh, lim, why)
    assert len(told) == 1 and told[0]["face"] == "Mid_1" and min(told[0]["angles"]) == 24.0, told
    assert told[0]["nodes"] == why[:3]


def test_each_edge_length_is_hard_only_where_the_user_said_so():
    """SHMESH-51o35: the user's word on each edge length, read in one place - "min 5 mm but flexible; max 15 mm is
    hard" sets each on its own; nothing said is flexible; the older whole-mesh switch makes both hard."""
    from meshpipeline.engines.gmsh import shell_adapter as SA

    rows = [{"field": "min_edge", "strictness": "flexible"}, {"field": "max_edge", "strictness": "hard"}]
    assert SA._edge_hard({"rows": rows}) == (False, True)
    assert SA._edge_hard({"rows": [{"field": "min_edge"}, {"field": "max_edge"}]}) == (False, False)
    assert SA._edge_hard({"edge_hard": True}) == (True, True)
    assert SA._edge_hard(None) == (False, False)


def test_a_cell_is_counted_on_its_targets_its_outer_limits_and_the_edge_band():
    """SHMESH-51o42: the counts a transition cell is set against its paving with - past the solver, past an outer
    limit or outside the flexible edge band, outside a target or edge length."""
    from meshpipeline.engines.gmsh import shell_adapter as SA

    xyz = np.array([[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0],            # a 10 mm square: inside everything
                    [20, 0, 0], [37, 0, 0], [37, 10, 0], [20, 10, 0],          # 17 mm long: over 15, inside the band
                    [40, 0, 0], [60, 0, 0], [60, 10, 0], [40, 10, 0]], dtype=float)  # 20 mm: past the 18 mm band
    quads = np.array([[0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 10, 11]])
    lim = sq.InForce(aims=())
    none_ = np.zeros((0, 3), dtype=np.int64)
    assert SA._cell_counts(xyz, quads[:1], none_, lim, 5.0, 15.0, (False, False)) == (0, 0, 0)
    assert SA._cell_counts(xyz, quads[1:2], none_, lim, 5.0, 15.0, (False, False)) == (0, 0, 1)
    assert SA._cell_counts(xyz, quads[1:2], none_, lim, 5.0, 15.0, (False, True)) == (0, 1, 1)   # hard: no band
    assert SA._cell_counts(xyz, quads[2:], none_, lim, 5.0, 15.0, (False, False))[1:] == (1, 1)


def test_the_polish_counts_each_line_apart_and_leaves_a_node_on_a_fold():
    """SHMESH-51o45: the counts a polish move or re-cut is held to are per line (one line's gain never pays for
    another's loss), and a node whose elements lie in two planes never moves."""
    from meshpipeline.engines.gmsh import shell_adapter as SA

    lim = sq.InForce(aims=(("quad", "warp_deg", "<=", 15.0, 30.0, 0.01),
                           ("tria", "min_angle_tria", ">=", 30.0, 20.0, 0.01)))
    xyz = np.array([[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0], [5, 5, 6]], dtype=float)
    flat, warped = np.array([[0, 1, 2, 3]]), np.array([[0, 1, 2, 4]])
    a = SA._line_counts(xyz, flat, "quad", lim)
    b = SA._line_counts(xyz, warped, "quad", lim)
    assert a.tolist()[:2] == [0, 0] and b.tolist()[:2] == [1, 1]          # the warp line: outside and past
    assert len(a) == 2 * len(lim.aims) + 4
    # a node with two quads in two planes (a fold) is not flat round
    xyz2 = np.array([[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0], [10, 0, 10], [0, 0, 10]], dtype=float)
    Q = np.array([[0, 1, 2, 3], [1, 0, 5, 4]])
    none_ = np.zeros((0, 3), np.int64)
    assert SA._flat_round(xyz2, Q, none_, [0], [])
    assert not SA._flat_round(xyz2, Q, none_, [0, 1], [])


def test_the_finer_pass_is_kept_in_the_ranked_order():
    # SHMESH-51o62: fewer past the solver's limits first; with those equal, fewer past an outer limit, or a line
    # met that the coarser pass missed (none lost)
    from meshpipeline.engines.gmsh import shell_adapter as sa

    def ap(beyond, met):
        return {"requirements": {"rows": [
            {"id": "r5", "field": "limit:aspect_ratio", "beyond": beyond, "met": "r5" in met},
            {"id": "r8", "field": "limit:min_angle", "beyond": 0, "met": "r8" in met}]}}

    assert sa._finer_kept(ap(9, set()), 1, ap(0, {"r5", "r8"}), 2)
    assert not sa._finer_kept(ap(0, {"r5", "r8"}), 2, ap(9, set()), 1)
    assert sa._finer_kept(ap(3, set()), 0, ap(4, {"r8"}), 0)
    assert sa._finer_kept(ap(3, {"r8"}), 0, ap(3, set()), 0)
    assert not sa._finer_kept(ap(3, {"r8"}), 0, ap(3, {"r5"}), 0)
    assert not sa._finer_kept(ap(3, set()), 0, ap(3, set()), 0)


def test_the_standing_record_shows_each_line_apart():
    from meshpipeline.engines.gmsh import shell_adapter as sa

    a = {"mesh": "A", "elements": 100, "lines": [{"id": "r5", "beyond": 3, "met": False},
                                                 {"id": "r10", "beyond": 9, "met": False}]}
    b = {"mesh": "B", "elements": 50, "lines": [{"id": "r5", "beyond": 8, "met": False},
                                                {"id": "r10", "beyond": 6, "met": False}]}
    got = sa._by_line(a, b)
    assert got["mixed"] is True
    assert got["lines"][0]["past_outer"] == {"A": 3, "B": 8}
    assert got["past_outer_share"] == {"A": 0.12, "B": 0.28}


def test_narrow_along_needs_the_switch_and_both_typed_edges(monkeypatch):
    # SHMESH-51o63 (lab): only with NARROW_ALONG on and a shortest and longest edge typed
    from meshpipeline.engines.gmsh import shell_adapter as sa

    plan = {"stretch": 3.6, "low_mm": 15.0, "high_mm": 45.0}
    monkeypatch.setattr(sa, "NARROW_ALONG", False)
    assert sa._along(plan) is None
    monkeypatch.setattr(sa, "NARROW_ALONG", True)
    assert sa._along(plan) == (3.6, 15.0, 45.0)
    assert sa._along({**plan, "low_mm": None}) is None
    rows = [{"size_mm": 30.0, "wall_mm": 8.0, "rule": "SHMESH-1"}]
    facts = [{"width": 6.0, "box_width": 0.0, "area": 600.0, "bend": None}]
    got = sa._narrow_faces(rows, facts, 1.0, 100.0, along=sa._along(plan))
    assert got is not None and rows[0]["size_mm"] == pytest.approx(21.6, rel=0.02)


def test_a_stretched_face_is_judged_in_the_ranked_order():
    # SHMESH-51o65: solver limits, then elements past an outer line (fewer: better, whatever the in-band counts do),
    # then an in-band rise only where it takes a line met at today's sizing to missed, then triangles
    import types

    from meshpipeline.engines.gmsh import shell_adapter as sa

    lim = types.SimpleNamespace(aims=(("quad", "max_angle", "<=", 135.0, 150.0, 0.01),))
    k3 = ("max_angle", "<=", 135.0)
    key_in, key_out = (*k3, "in"), (*k3, "out")
    base = {"trias": 4, "solver": 0, key_in: 3, key_out: 2}
    met, missed = {k3: True}, {k3: False}
    assert sa._narrow_worse(dict(base), base, met, met) is None
    assert sa._narrow_worse({**base, "solver": 1}, base, met, met)[0] == "a"
    assert sa._narrow_worse({**base, key_out: 3}, base, met, met)[0] == "b"
    # fewer past the outer line: better, the in-band rise and the triangles do not count
    assert sa._narrow_worse({**base, key_out: 0, key_in: 90, "trias": 9}, base, missed, met) is None
    assert sa._narrow_worse({**base, key_in: 9}, base, missed, met)[0] == "c"
    assert sa._narrow_worse({**base, key_in: 9}, base, missed, missed) is None
    assert sa._narrow_worse({**base, "trias": 5}, base, met, met)[0] == "d"
    assert sa._whole_met({key_in: 10, key_out: 0}, 1000, lim) == {k3: True}
    assert sa._whole_met({key_in: 11, key_out: 0}, 1000, lim) == {k3: False}


def test_mid_faces_sharing_an_edge_are_neighbours():
    from meshpipeline.engines.gmsh import shell_adapter as sa
    from meshpipeline.engines.shell_mesh import ShellMesh

    xyz = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [2, 0, 0], [2, 1, 0], [3, 3, 0]], dtype=float)
    mesh = ShellMesh(xyz=xyz, quads=np.array([[0, 1, 2, 3], [1, 4, 5, 2]]), trias=np.array([[5, 6, 4]]),
                     wall=np.ones(3), mid=["Mid_1", "Mid_2", "Mid_3"], part=["Part_1"] * 3)
    nb = sa._mid_neighbours(mesh)
    assert nb["Mid_1"] == {"Mid_2"} and nb["Mid_2"] == {"Mid_1", "Mid_3"}


def test_narrow_along_stands_aside_where_the_stamped_strip_rules_own_the_faces(monkeypatch):
    # SHMESH-51o68: a surface file (or STAMPED_ALL on) - the stamped-sheet strip rules size its narrow faces
    from meshpipeline.engines.gmsh import shell_adapter as sa

    monkeypatch.setattr(sa, "NARROW_ALONG", True)
    plan = {"stretch": 3.6, "low_mm": 15.0, "high_mm": 45.0}
    assert sa._along(plan, {"source": "solid"}) == (3.6, 15.0, 45.0)
    assert sa._along(plan, {"source": "surface"}) is None
    monkeypatch.setattr(sa, "STAMPED_ALL", True, raising=False)
    assert sa._along(plan, {"source": "solid"}) is None


# ------------------------------------------------------------------ SHMESH-31b: the spot plan held ----------------
def _tapered_quads(n: int, gap: float = 10.0) -> tuple[np.ndarray, np.ndarray]:
    # n quads on one face, far apart, each tapered more than the one before (past Nastran's taper, worse and worse)
    pts, quads = [], []
    for i in range(n):
        x0 = i * gap
        t = 0.3 - 0.06 * i                       # the far side shrinks: taper grows with i
        pts += [[x0, 0, 0], [x0 + 1, 0, 0], [x0 + 0.5 + t / 2, 1, 0], [x0 + 0.5 - t / 2, 1, 0]]
        quads.append([4 * i, 4 * i + 1, 4 * i + 2, 4 * i + 3])
    return np.array(pts, dtype=float), np.array(quads, dtype=np.int64)


def test_past_the_box_cap_the_worst_spots_are_kept_and_no_box_spans_a_face(monkeypatch):
    from meshpipeline.engines.gmsh import shell_adapter as sa
    from meshpipeline.engines.shell_mesh import ShellMesh

    xyz, q = _tapered_quads(5)
    mesh = ShellMesh(xyz=xyz, quads=q, trias=np.zeros((0, 3), dtype=np.int64), wall=np.ones(5), mid=["Mid_1"] * 5,
                     part=["Part_1"] * 5)
    past = np.ones(5, dtype=bool)
    monkeypatch.setattr(sa, "SPOTS_MAX", 2)
    boxes, left = sa._spots(mesh, np.zeros(5, dtype=np.int64), past)
    assert len(boxes) == 2 and left == 3
    # the two kept are the two worst (the last two quads), each its own small box - not one box over the face
    xs = sorted(lo[0] for _k, lo, _hi, _reach in boxes)
    assert xs[0] > 25.0 and all(hi[0] - lo[0] < 5.0 for _k, lo, hi, _reach in boxes)
    # SHMESH-31e: each box carries its elements' longest edge (its reach)
    assert all(reach > 1.0 for *_b, reach in boxes)


def test_a_spot_box_grows_to_its_reach_only_where_its_pass_left_an_element_past_within_it(monkeypatch):
    from meshpipeline.engines.gmsh import shell_adapter as sa

    own = [(3, (0.0, 0.0, 0.0), (1.0, 1.0, 0.0), 0, 0.8, False),     # an element past 0.5 mm beyond it: grown
           (3, (10.0, 0.0, 0.0), (11.0, 1.0, 0.0), 0, 0.8, False)]   # nothing past within 0.8 mm: stands
    past = [(3, (1.5, 0.2, 0.0), (1.7, 0.4, 0.0), 0.3)]
    got = sa._grown(own, past)
    assert [g[5] for g in got] == [True, False] and got[0][:5] == own[0][:5]
    assert sa._grown(got, []) == got                                 # a grown box stays grown
    monkeypatch.setattr(sa, "SPOT_GROW", False)
    assert [g[5] for g in sa._grown(own, past)] == [False, False]


def test_the_steps_and_the_products_check_share_one_margin_at_a_solver_limit():
    # SHMESH-89e: an element at a solver limit, or within 1e-6 of it, is past for the steps and for the record alike
    from meshpipeline.engines.shell_mesh import ShellMesh

    v = np.array([0.5, 0.5 - 2e-7, 0.5 - 2e-6, 0.4])
    assert sq.solver_past(v, "<=", 0.5).tolist() == [True, True, False, False]
    assert sq.solver_past(np.array([10.0, 10.000005, 10.0001]), ">=", 10.0).tolist() == [True, True, False]
    rng = np.random.default_rng(2)
    pts, quads = [], []
    for i in range(40):
        b = 4 * len(quads)
        x = 3.0 * i
        t = 0.1 + 0.02 * i                     # tapers from far inside to past Q4_TAPER
        pts += [[x, 0, 0], [x + 1, 0, 0], [x + 0.5 + t / 2, 1, 0], [x + 0.5 - t / 2, 1, 0]]
        quads.append([b, b + 1, b + 2, b + 3])
    xyz = np.array(pts) + rng.normal(0, 1e-7, (len(pts), 3)) * np.array([1, 1, 0])
    q = np.array(quads, dtype=np.int64)
    mesh = ShellMesh(xyz=xyz, quads=q, trias=np.zeros((0, 3), dtype=np.int64), wall=np.ones(len(q)),
                     mid=["Mid_1"] * len(q), part=["Part_1"] * len(q))
    steps = sq._quad_past(sq.quad_measures(xyz, q))
    assert steps.any() and not steps.all()
    assert sq.nastran_past(mesh).tolist() == steps.tolist()
    assert sum(sq.measure(mesh)["nastran_past"].values()) >= int(steps.sum())


def test_an_optional_trial_starts_only_after_the_solver_limit_pass_is_set_aside(monkeypatch):
    # SHMESH-31f, on a fake clock: deadline at 1000 s, a whole mesh costs 200 s, TIME_MARGIN_S 30 s
    from meshpipeline.engines.gmsh import shell_adapter as sa

    now = [500.0]
    monkeypatch.setattr(sa.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(sa, "TIME_MARGIN_S", 30.0)
    monkeypatch.setitem(sa._RUN, "end", 1000.0)
    monkeypatch.setitem(sa._RUN, "first_s", 200.0)
    req = {"limits": []}
    assert sa._affords_trial(3, req)            # 470 s left: the trial (200) and the spot pass kept for (200) fit
    now[0] = 600.0                              # 370 s left
    assert not sa._affords_trial(3, req)        # the spot pass is owed: no trial
    assert sa._affords_trial(0, req)            # nothing past the solver's limits: the trial alone fits
    assert sa._affords(200.0)                   # (the spot pass itself still fits)
    now[0] = 800.0                              # 170 s left: no whole mesh at all
    assert not sa._affords_trial(0, req)
    monkeypatch.setitem(sa._RUN, "end", None)   # no deadline: always
    assert sa._affords_trial(3, req)


def test_the_shell_mesh_takes_what_is_left_of_the_runs_outer_stop(monkeypatch):
    # SHMESH-31g, on a fake clock: the runner's stop at t = 10,000 s; the mesh's own limit 900 s, 60 s kept for the decks
    from meshpipeline.engines.gmsh import shell_adapter as sa

    monkeypatch.setattr(sa, "MESH_DEADLINE_S", 900.0)
    monkeypatch.setattr(sa, "END_MARGIN_S", 60.0)
    monkeypatch.setattr(sa.time, "time", lambda: 9_000.0)
    monkeypatch.delenv(sa.SHELL_END_ENV, raising=False)
    assert sa._deadline_s() == 900.0                       # no outer stop told: the mesh's own 900 s
    monkeypatch.setenv(sa.SHELL_END_ENV, "10000")
    assert sa._deadline_s() == 900.0                       # 1,000 s left: at most 900
    monkeypatch.setattr(sa.time, "time", lambda: 9_400.0)
    assert sa._deadline_s() == 540.0                       # 600 s left, 60 for the decks
    monkeypatch.setattr(sa.time, "time", lambda: 9_990.0)
    assert sa._deadline_s() == 1.0                         # past it: the least, never none
    monkeypatch.setenv(sa.SHELL_END_ENV, "not a time")
    assert sa._deadline_s() == 900.0


def test_when_each_mesh_is_worse_on_a_line_the_fewer_past_in_total_is_kept():
    # SHMESH-36b, fuselage_01's figures: this PR's mesh has 1 triangle 0.13 degrees past the 140-degree line and 6
    # past the 20-degree one; #435's mesh none past 140 and 74 past 20 - each worse on a line
    from meshpipeline.engines.gmsh import shell_adapter as sa

    req = {"limits": [{"id": "r9", "op": "<=", "target": 120.0, "outer": 140.0},
                      {"id": "r10", "op": ">=", "target": 30.0, "outer": 20.0}]}

    def cand(b9, w9, b10, w10, long_met):
        return {"requirements": {"rows": [{"id": "r9", "field": "limit:r9", "beyond": b9, "worst": w9, "met": not b9},
                                          {"id": "r10", "field": "limit:r10", "beyond": b10, "worst": w10,
                                           "met": False},
                                          {"id": "s2", "field": "size:high", "met": long_met}]}}

    # (this PR's mesh meets the longest edge, #435's has 2 elements over it: one line met each)
    mine, theirs = cand(1, 140.133, 6, 13.86, True), cand(0, 139.6, 74, 15.0, False)
    s_mine, s_theirs = sa._standing(mine, 0, theirs, req), sa._standing(theirs, 0, mine, req)
    assert s_theirs < s_mine                               # line by line alone: #435's would be kept
    assert sa._each_worse(mine, theirs)                    # but each is worse on a line (r9 1 vs 0, r10 6 vs 74)
    k_mine, k_theirs = sa._mixed_key(mine, req), sa._mixed_key(theirs, req)
    assert k_mine[0] == k_theirs[0] == -1                  # a tie on lines met
    assert k_mine < k_theirs and k_mine[1] == 7 and k_theirs[1] == 74   # in total: this PR's
    assert k_mine[3] == pytest.approx(0.614, abs=1e-3)     # (20 - 13.86) / 10, the larger overshoot
    # a mesh meeting more of the user's lines wins first (sh0114: 15 past in total against 18, but a line missed more)
    more = {"requirements": {"rows": theirs["requirements"]["rows"] + [{"id": "s1", "field": "size:high", "met": True}]}}
    assert sa._mixed_key(more, req) < k_mine
    assert not sa._each_worse(mine, cand(1, 141.0, 9, 12.0, True))   # no worse anywhere: decided as before


def test_the_whole_allowance_is_more_than_the_reserve_for_moves_back_under_the_longest_edge():
    # SHMESH-86g: 100 unit quads, 2% allowed in the quad-max band, none in it yet: 0.8 of it is 1 quad, the whole 2
    xyz = np.array([[i, j, 0.0] for j in range(11) for i in range(11)], dtype=float)
    quads = np.array([[j * 11 + i, j * 11 + i + 1, (j + 1) * 11 + i + 1, (j + 1) * 11 + i]
                      for j in range(10) for i in range(10)], dtype=np.int64)
    lim = sq.InForce(aims=(("quad", "max_angle_quad", "<=", 135.0, 150.0, 0.02),))
    col = 1 + len(lim.hard)
    reserve = sq.line_slack(xyz, quads, np.zeros((0, 3), dtype=np.int64), lim)
    whole = sq.line_slack(xyz, quads, np.zeros((0, 3), dtype=np.int64), lim, margin=1.0)
    assert int(reserve[col]) == 1 and int(whole[col]) == 2
    assert sq.LONG_FULL_ALLOWANCE


def test_a_bounded_call_returns_or_is_stopped_in_words():
    import time

    from meshpipeline.engines.gmsh import shell_adapter as sa

    assert sa._bounded(lambda: 7, 5.0) == 7
    assert sa._bounded(lambda: 8, None) == 8
    with pytest.raises(ValueError, match="SHMESH-31b"):
        sa._bounded(lambda: time.sleep(5), 0.5)


def _bounded_in_a_daemon(q) -> None:
    import time

    from meshpipeline.engines.gmsh import shell_adapter as sa

    got = [sa._bounded(lambda: 9, 5.0)]
    try:
        sa._bounded(lambda: time.sleep(5), 0.5)
    except ValueError as exc:
        got.append(str(exc))
    q.put(got)


def test_a_bounded_call_works_where_the_shell_mesh_runs_in_a_daemonic_child():
    # SHMESH-31b: the shell mesh itself runs in a daemonic multiprocessing child (SHMESH-21), which may not start
    # multiprocessing children: the bound is a plain fork
    import multiprocessing as mp

    ctx = mp.get_context("fork")
    q = ctx.Queue()
    proc = ctx.Process(target=_bounded_in_a_daemon, args=(q,), daemon=True)
    proc.start()
    got = q.get(timeout=30)
    proc.join(timeout=30)
    assert got[0] == 9 and "SHMESH-31b" in got[1]


def test_the_run_clock_gives_a_pass_the_time_left_less_the_margin(monkeypatch):
    # SHMESH-31b: a spot pass or a trial runs under the time left to the deadline less what must follow it, and one
    # priced over that (the first mesh's own time) is not started
    import time

    from meshpipeline.engines.gmsh import shell_adapter as sa

    monkeypatch.setattr(sa, "TIME_MARGIN_S", 30.0)
    monkeypatch.setitem(sa._RUN, "end", time.monotonic() + 100.0)
    assert 69.0 < sa._limit() <= 70.0
    assert sa._affords(50.0) and sa._affords(None) and not sa._affords(80.0)
    monkeypatch.setitem(sa._RUN, "end", time.monotonic() + 10.0)
    assert not sa._affords(None)                     # nothing left after the margin
    monkeypatch.setitem(sa._RUN, "end", None)
    assert sa._limit() is None and sa._affords(1e9)
    with pytest.raises(ValueError, match="stopped"):
        sa._bounded(lambda: 1, 0.0)


def _plate_mesh():
    from meshpipeline.engines.shell_mesh import ShellMesh

    xyz, q = _tapered_quads(1)
    return ShellMesh(xyz=xyz, quads=q, trias=np.zeros((0, 3), dtype=np.int64), wall=np.ones(1), mid=["Mid_1"],
                     part=["Part_1"])


def test_after_a_cut_spot_pass_no_whole_mesh_more_is_started(monkeypatch):
    # SHMESH-31b: once a spot pass was stopped at the deadline, SHMESH-36's #435 mesh is not tried: the mesh made
    # stands and the record says why; the run's clock stops when the shell mesh returns
    from meshpipeline.engines.gmsh import shell_adapter as sa

    def guarded(*_a, **_k):
        sa._RUN["cut"] = True
        return _plate_mesh(), {"quality": {"valid": True, "nastran_past": {"taper": 1}}}, 1

    def never(*_a, **_k):
        raise AssertionError("a whole mesh more was started")

    monkeypatch.setattr(sa, "_guarded_path", guarded)
    monkeypatch.setattr(sa, "_mesh_path", never)
    _mesh, applied = sa._mesh_shell("x.brep", {}, requirements={"rows": [{"id": "r1"}]})
    assert "SHMESH-31b" in applied["kept"]["why"] and applied["kept"]["candidates"][0]["past_solver"] == 1
    assert sa._RUN == {"end": None, "first_s": None, "cut": False}


def test_a_trial_priced_past_the_time_left_is_not_started(monkeypatch):
    # SHMESH-31b: SHMESH-45's mesh without the structured steps is priced at the first mesh's own time
    from meshpipeline.engines.gmsh import shell_adapter as sa

    calls = []

    def path(*_a, **_k):
        calls.append(1)
        if len(calls) > 1:
            raise AssertionError("a whole mesh more was started")
        sa._RUN["first_s"] = 1e6                     # the first mesh took longer than the time left
        return _plate_mesh(), {"quality": {"valid": True, "nastran_past": {"taper": 1}}}, 1

    monkeypatch.setattr(sa, "_structured_on", lambda _r: True)
    monkeypatch.setattr(sa, "_mesh_path", path)
    monkeypatch.setitem(sa._RUN, "end", __import__("time").monotonic() + 600.0)
    _mesh, applied, n = sa._guarded_path("x.brep", {}, requirements={"rows": []})
    assert n == 1 and len(calls) == 1
    assert "SHMESH-31b" in applied["quality"]["structure"]["guard"]["why"]


def test_rings_under_the_shortest_edge_go_back_only_where_worse_in_the_ranked_order():
    # SHMESH-51o74: s0054's long plate (INT5) - 5 in a band against 4 on one line, met either way; better on triangles,
    # another band and the outer lines: no hole back. Past the solver's limits first; a band rise counts only where it
    # takes a line the whole mesh meets with one ring to missed; then triangles
    import types

    from meshpipeline.engines.gmsh import shell_adapter as sa

    aims = tuple((("quad" if i < 4 else "tria"), f"k{i}", "<=", 1.0, 2.0, 0.01) for i in range(8))
    lim = types.SimpleNamespace(aims=aims)
    vw = np.array([26, 0, 0, 0, 0, 0, 4, 0, 5, 0, 0, 0, 0, 0, 0, 0, 0, 0])
    vb = np.array([48, 0, 0, 0, 2, 0, 14, 0, 4, 0, 2, 0, 2, 0, 0, 0, 2, 2])
    whole = np.array([300, 0, 10, 0, 10, 0, 40, 0, 60, 0, 10, 0, 10, 0, 0, 0, 0, 0])
    assert sa._ul_worse(vw, vb, whole, 43000, lim) == []
    assert sa._ul_worse(vw + np.eye(18, dtype=int)[1], vb, whole, 43000, lim) == [1]
    # a band rise that takes a met line to missed sends holes back; one that leaves it met does not
    vw2, vb2 = vb.copy(), vb.copy()
    vw2[8] = 9
    assert sa._ul_worse(vw2, vb2, whole, 5600, lim) == [8]     # 60 past 1% of 5600 with two rings, 55 with one
    assert sa._ul_worse(vw2, vb2, whole, 43000, lim) == []
    vw3 = vb.copy()
    vw3[0] = 60
    assert sa._ul_worse(vw3, vb, whole, 43000, lim) == [0]
    # a line past its outer line with two rings where one ring has none
    vw4 = vb.copy()
    vw4[3] = 1
    assert sa._ul_worse(vw4, vb, whole, 43000, lim) == [3]


def test_the_one_ring_trial_asks_the_shared_pricing_by_name(monkeypatch):
    # SHMESH-31f's _affords_trial where the tree has it (the integration tree); else the trial starts, as before
    from meshpipeline.engines.gmsh import shell_adapter as sa

    monkeypatch.delitem(sa.__dict__, "_affords_trial", raising=False)
    assert sa._trial_ok(3, {"limits": []}) is True
    seen = []
    monkeypatch.setitem(sa.__dict__, "_affords_trial", lambda past_n, req: seen.append(past_n) or False)
    assert sa._trial_ok(5, {"limits": []}) is False and seen == [5]
    assert "SHMESH-31f" in sa._trial_why()
