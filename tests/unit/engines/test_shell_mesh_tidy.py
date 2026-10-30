# Responsibility: Hold the shell mesh's triangle clean-up to its rules (SHMESH-12 fold guard, SHMESH-14b, -17, -18,
# -19): two triangles that make a good quad become it, a lone triangle walks through the quads of its face to meet
# another, every face's boundary has an even number of segments before Blossom, the relaxation also tries the
# parallelogram point, and a sliver merge that folds one quad is undone at that quad alone.
# Boundaries: numpy on small made-up meshes, and gmsh/OpenCASCADE in this process on a made-up plate; no solver.
from __future__ import annotations

import numpy as np

from meshpipeline.engines import shell_quality as sq


def _grid(nx: int, ny: int) -> tuple[np.ndarray, np.ndarray]:
    xyz = np.array([[i, j, 0.0] for j in range(ny + 1) for i in range(nx + 1)], dtype=float)
    quads = np.array([[j * (nx + 1) + i, j * (nx + 1) + i + 1, (j + 1) * (nx + 1) + i + 1, (j + 1) * (nx + 1) + i]
                      for j in range(ny) for i in range(nx)], dtype=np.int64)
    return xyz, quads


def _normals_up(xyz: np.ndarray, quads: np.ndarray) -> bool:
    n = np.cross(xyz[quads[:, 2]] - xyz[quads[:, 0]], xyz[quads[:, 3]] - xyz[quads[:, 1]])
    return bool((n[:, 2] > 0).all())


def test_two_triangles_that_make_a_good_quad_become_it():
    xyz, quads = _grid(2, 1)
    Q = quads[[0]]
    T = np.array([[1, 2, 5], [1, 5, 4]], dtype=np.int64)      # the right cell cut on its diagonal
    new_q, kept, came = sq.pair_trias(xyz, Q, T, [0, 0])
    assert len(new_q) == 1 and len(kept) == 0 and came.tolist() == [0]
    assert not sq._quad_past(sq.quad_measures(xyz, new_q)).any() and _normals_up(xyz, new_q)
    # never across two faces
    new_q2, kept2, _c = sq.pair_trias(xyz, Q, T, [0, 1])
    assert len(new_q2) == 0 and kept2.tolist() == [0, 1]


def _hexagon():
    # a triangle each side of one quad: 0 (0.5, 0.5) | 1 (1, 0), 2 (1, 1) | 3 (2, 0), 4 (2, 1) | 5 (2.5, 0.5)
    xyz = np.array([[0.5, 0.5, 0], [1, 0, 0], [1, 1, 0], [2, 0, 0], [2, 1, 0], [2.5, 0.5, 0]], dtype=float)
    return xyz, np.array([[1, 3, 4, 2]], dtype=np.int64), np.array([[0, 1, 2], [3, 5, 4]], dtype=np.int64)


def test_a_lone_triangle_walks_through_a_quad_to_meet_the_other():
    xyz, Q, T = _hexagon()
    assert len(sq.pair_trias(xyz, Q, T, [0, 0])[0]) == 0         # not neighbours: pairing alone cannot
    Q2, T2, qf, tf, walked = sq.walk_trias(xyz, Q, T, [0], [0, 0])
    assert walked == 1 and len(T2) == 0 and len(Q2) == 2 and qf == [0, 0] and tf == []
    assert not sq._quad_past(sq.quad_measures(xyz, Q2)).any() and _normals_up(xyz, Q2)
    # the same nodes, none moved, and the same area
    assert set(Q2.ravel().tolist()) == set(range(6))
    area = sum(0.5 * abs(np.cross(xyz[q[2]] - xyz[q[0]], xyz[q[3]] - xyz[q[1]])[2]) for q in Q2)
    assert abs(area - 1.5) < 1e-12


def test_a_walk_that_would_leave_a_quad_past_a_limit_is_not_made():
    xyz, Q, T = _hexagon()
    xyz[0] = [0.0, 0.5, 0.0]                                       # the quad the walk would leave: too tapered
    Q2, T2, _qf, _tf, walked = sq.walk_trias(xyz, Q, T, [0], [0, 0])
    assert walked == 0 and Q2.tolist() == Q.tolist() and T2.tolist() == T.tolist()
    # nor across two faces
    xyz, Q, T = _hexagon()
    assert sq.walk_trias(xyz, Q, T, [0], [0, 1])[4] == 0


def test_the_walk_is_deterministic():
    xyz, Q, T = _hexagon()
    a = sq.walk_trias(xyz, Q, T, [0], [0, 0])
    b = sq.walk_trias(xyz, Q, T, [0], [0, 0])
    assert np.array_equal(a[0], b[0]) and np.array_equal(a[1], b[1]) and a[4] == b[4]


def test_the_parallelogram_point_mends_what_the_middle_cannot():
    # a 2 x 1 strip whose shared edge's top node 4 sits far right: the two quads past the taper limit
    xyz, quads = _grid(2, 1)
    xyz[4] = [1.9, 1.0, 0.0]
    none = np.zeros((0, 3), dtype=np.int64)
    movable = np.zeros(len(xyz), dtype=bool)
    movable[4] = True
    assert sq._quad_past(sq.quad_measures(xyz, quads)).any()
    moved, n = sq.relax_past(xyz, quads, none, movable, parallelogram=True)
    assert n >= 1 and not sq._quad_past(sq.quad_measures(moved, quads)).any()
    assert moved[4, 2] == 0.0


def test_every_face_of_a_holed_plate_gets_an_even_boundary_and_few_triangles(tmp_path):
    from OCP.BRepTools import BRepTools
    from tests.fea_fixtures import _box, _cut, _cyl

    from meshpipeline.cad import midsurface as ms
    from meshpipeline.engines.gmsh import shell_adapter as sa

    shape = _box(0, 0, 0, 400, 250, 2.0)
    for x, y, r in [(60, 60, 7.0), (200, 90, 11.0), (320, 180, 5.5), (120, 190, 9.0), (300, 60, 4.0)]:
        shape = _cut(shape, _cyl(x, y, -1, r, 4.0))
    rec, surface = ms.extract(shape, mm_per_unit=1.0)
    brep = tmp_path / "plate.brep"
    BRepTools.Write_s(surface, str(brep))
    mesh, applied = sa.mesh_shell(brep, rec)
    q = applied["quality"]
    assert q["valid"] and q["within_nastran"], q["nastran_past"]
    assert q["tria_share"] <= 0.02, q["tria_share"]
    old = sa.EVEN_FACES
    try:
        sa.EVEN_FACES = False
        _m, off = sa.mesh_shell(brep, rec)
    finally:
        sa.EVEN_FACES = old
    assert q["trias"] <= off["quality"]["trias"]


def test_a_sliver_merge_that_folds_a_quad_corner_is_undone_and_nothing_folds():
    from meshpipeline.engines.shell_mesh import collapse_short

    # node 6 sits 0.002 from node 1 and a little inside quad A; merging it into node 1 would fold A's corner. A far
    # sliver (node 10, 0.002 from node 8, a triangle between) harms nothing and still closes.
    xyz = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [2, 0, 0], [2, 1, 0], [1.002, 0.0015, 0],
                    [5, 0, 0], [6, 0, 0], [6, 1, 0], [6.002, 0, 0], [5, 1, 0], [7, 0, 0], [7, 1, 0]], dtype=float)
    quads = np.array([[0, 6, 2, 3], [6, 4, 5, 2], [7, 8, 9, 11], [10, 12, 13, 9]], dtype=np.int64)
    Q, T, _fq, _ft, merged = collapse_short(xyz, quads, np.array([[8, 10, 9]]), [0, 0, 0, 0], [0], np.array([0.5]))
    used = set(Q.ravel().tolist()) | set(T.ravel().tolist())
    assert merged >= 1 and not ({8, 10} <= used)
    if len(Q):
        assert (sq.quad_measures(xyz, Q)["min_jacobian"] > 0).all()
    if len(T):
        assert (sq.tria_measures(xyz, T)["min_jacobian"] > 0).all()
