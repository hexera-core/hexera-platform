# Responsibility: Hold the shell mesh's last clean-up and the sizes beside small pieces of the CAD to their rules (SHMESH-27, -28, -29): under the user's limits an element outside one is mended by merging a node away or cutting two neighbours' outline again - a node inside a face may go, a node on a CAD edge only along a straight piece of it, a node at a CAD vertex never, unless the user asked for edges under a length to be ignored; no change makes an element that is not valid; with no limits and nothing past a solver limit nothing changes; an element may be at most 2.5 times as long as the smallest piece of the CAD it touches.
# Boundaries: numpy on small made-up meshes; no gmsh, no solver.
from __future__ import annotations

import numpy as np

from meshpipeline.engines import shell_plan as sp
from meshpipeline.engines import shell_quality as sq

#: the usual triangle line: smallest corner over 30 degrees, 1% between 20 and 30, none under 20
AIMS = sq.InForce(aims=(("tria", "min_angle_tria", ">=", 30.0, 20.0, 0.01),
                        ("tria", "max_angle_tria", "<=", 120.0, 140.0, 0.01),
                        ("quad", "min_angle_quad", ">=", 45.0, 30.0, 0.01),
                        ("quad", "max_angle_quad", "<=", 135.0, 150.0, 0.01)))
IN, EDGE, VERTEX = sq.INSIDE, sq.ON_EDGE, sq.AT_VERTEX


def _area(xyz, Q, T) -> float:
    a = sum(0.5 * abs(np.cross(xyz[q[2]] - xyz[q[0]], xyz[q[3]] - xyz[q[1]])[2]) for q in Q)
    return a + sum(0.5 * abs(np.cross(xyz[t[1]] - xyz[t[0]], xyz[t[2]] - xyz[t[0]])[2]) for t in T)


def _cost(xyz, Q, T, limits=AIMS) -> int:
    cq, ct, _a, _b = sq._costs(xyz, Q, T, limits)
    return int(cq.sum() + ct.sum())


def _needle_inside():
    """Four cells round a centre node 4, with a second node 9 just right of it: the two right cells are each a quad
    and a needle triangle (8.5 degrees)."""
    xyz = np.array([[0, 0, 0], [1, 0, 0], [2, 0, 0], [0, 1, 0], [1, 1, 0], [2, 1, 0], [0, 2, 0], [1, 2, 0], [2, 2, 0],
                    [1.15, 1, 0]], dtype=float)
    Q = np.array([[0, 1, 4, 3], [3, 4, 7, 6], [1, 2, 5, 9], [9, 5, 8, 7]], dtype=np.int64)
    T = np.array([[1, 9, 4], [4, 9, 7]], dtype=np.int64)
    kind = np.array([VERTEX, EDGE, VERTEX, EDGE, IN, EDGE, VERTEX, EDGE, VERTEX, IN], dtype=np.int8)
    edge = np.array([-1, 10, -1, 13, -1, 11, -1, 12, -1, -1], dtype=np.int64)
    ends = {0: {10, 13}, 2: {10, 11}, 8: {11, 12}, 6: {12, 13}}
    return xyz, Q, T, kind, edge, ends


def test_a_needle_inside_a_face_is_merged_away():
    xyz, Q, T, kind, edge, ends = _needle_inside()
    assert _cost(xyz, Q, T) == 2 * sq.AIM_OUTER
    Q2, T2, qf, tf, done = sq.mend(xyz, Q, T, [0] * 4, [0] * 2, kind, edge, ends, limits=AIMS)
    assert done == {"merged": 1, "recut": 0} and len(T2) == 0 and len(Q2) == 4 and qf == [0] * 4 and tf == []
    assert _cost(xyz, Q2, T2) == 0 and abs(_area(xyz, Q2, T2) - 4.0) < 1e-12
    # every node on the outline is still a corner of the mesh; one of the two inside nodes is gone
    used = set(Q2.ravel().tolist())
    assert set(range(9)) - {4} <= used and len({4, 9} & used) == 1


def test_with_no_user_limit_and_nothing_past_a_solver_limit_nothing_changes():
    xyz, Q, T, kind, edge, ends = _needle_inside()
    Q2, T2, qf, tf, done = sq.mend(xyz, Q, T, [0] * 4, [0] * 2, kind, edge, ends)
    assert done == {"merged": 0, "recut": 0}
    assert Q2.tolist() == Q.tolist() and T2.tolist() == T.tolist() and qf == [0] * 4 and tf == [0] * 2


def _strip(drop: float = 0.0, kinds=(VERTEX, VERTEX), gap: float = 0.1):
    """Two quads along a free edge with a needle between them: bottom nodes 0, 1, 2, 3 (1 and 2 are `gap` apart),
    top nodes 4, 5, 6. `drop` lowers node 2 off the line; `kinds` are the kinds of nodes 1 and 2."""
    xyz = np.array([[0, 0, 0], [1, 0, 0], [1 + gap, -drop, 0], [2 + gap, 0, 0], [0, 1, 0], [1 + gap / 2, 1, 0],
                    [2 + gap, 1, 0]], dtype=float)
    Q = np.array([[0, 1, 5, 4], [2, 3, 6, 5]], dtype=np.int64)
    T = np.array([[1, 2, 5]], dtype=np.int64)
    kind = np.array([VERTEX, kinds[0], kinds[1], VERTEX, VERTEX, EDGE, VERTEX], dtype=np.int8)
    edge = np.array([-1, 10 if kinds[0] == EDGE else -1, 10 if kinds[1] == EDGE else -1, -1, -1, 20, -1],
                    dtype=np.int64)
    if kinds == (VERTEX, VERTEX):
        ends = {0: {10}, 1: {10, 11}, 2: {11, 12}, 3: {12}, 4: {20}, 6: {20}}
    else:
        ends = {0: {10}, 3: {10}, 4: {20}, 6: {20}}
        if kinds[0] == VERTEX:
            ends[1] = {10}
        if kinds[1] == VERTEX:
            ends[2] = {10}
    return xyz, Q, T, kind, edge, ends


def test_the_ends_of_a_short_cad_edge_stay_unless_the_user_asked_to_ignore_it():
    xyz, Q, T, kind, edge, ends = _strip()
    Q2, T2, _qf, _tf, done = sq.mend(xyz, Q, T, [0, 0], [0], kind, edge, ends, limits=AIMS)
    assert done["merged"] == 0 and {1, 2} <= set(Q2.ravel().tolist()) | set(T2.ravel().tolist())
    assert abs(_area(xyz, Q2, T2) - _area(xyz, Q, T)) < 1e-12
    # "ignore edges under 0.2 mm": the 0.1 mm edge's ends become one node, and the needle is gone
    Q3, T3, _qf, _tf, done3 = sq.mend(xyz, Q, T, [0, 0], [0], kind, edge, ends, limits=AIMS, suppress=0.2)
    assert done3["merged"] == 1 and len(T3) == 0 and len(Q3) == 2 and _cost(xyz, Q3, T3) == 0
    # ... but not an edge as long as the length they gave
    _Q4, T4, _qf, _tf, done4 = sq.mend(xyz, Q, T, [0, 0], [0], kind, edge, ends, limits=AIMS, suppress=0.1)
    assert done4["merged"] == 0 and len(T4) == 1


def test_a_node_on_a_cad_edge_goes_only_along_a_straight_piece_of_it():
    xyz, Q, T, kind, edge, ends = _strip(kinds=(EDGE, EDGE))
    Q2, T2, _qf, _tf, done = sq.mend(xyz, Q, T, [0, 0], [0], kind, edge, ends, limits=AIMS)
    assert done["merged"] == 1 and len(T2) == 0 and _cost(xyz, Q2, T2) == 0
    assert abs(_area(xyz, Q2, T2) - _area(xyz, Q, T)) < 1e-12              # the edge is where it was
    # the same nodes on a bent edge (node 2 stands 0.05 off the line): merging either would move the edge
    xyz, Q, T, kind, edge, ends = _strip(drop=0.05, kinds=(EDGE, EDGE))
    _Q3, T3, _qf, _tf, done3 = sq.mend(xyz, Q, T, [0, 0], [0], kind, edge, ends, limits=AIMS)
    assert done3["merged"] == 0 and len(T3) == 1


def test_two_triangles_are_cut_along_the_other_diagonal_when_that_mends_them():
    # a kite cut along its long diagonal: two triangles of 14 degrees; along the short one: two of 76 / 28
    xyz = np.array([[0, 0, 0], [2, -0.25, 0], [4, 0, 0], [2, 0.25, 0]], dtype=float)
    T = np.array([[0, 1, 2], [0, 2, 3]], dtype=np.int64)
    kind = np.full(4, VERTEX, dtype=np.int8)
    limits = sq.InForce(aims=(("tria", "max_angle_tria", "<=", 120.0, 140.0, 0.01),))
    Q0 = np.zeros((0, 4), dtype=np.int64)
    assert _cost(xyz, Q0, T, limits) == 2 * sq.AIM_OUTER
    Q2, T2, _qf, tf, done = sq.mend(xyz, Q0, T, [], [0, 0], kind, np.full(4, -1), {}, limits=limits,
                                    quads_too=False)
    assert done == {"merged": 0, "recut": 1} and len(Q2) == 0 and len(T2) == 2 and tf == [0, 0]
    assert _cost(xyz, Q2, T2, limits) == 0 and abs(_area(xyz, Q2, T2) - 1.0) < 1e-12
    # never across two faces
    _Q3, T3, _qf, _tf, done3 = sq.mend(xyz, Q0, T, [], [0, 1], kind, np.full(4, -1), {}, limits=limits)
    assert done3["recut"] == 0 and T3.tolist() == T.tolist()


def test_no_change_makes_an_element_that_is_not_valid_and_the_outline_stays():
    rng = np.random.default_rng(7)
    n = 9
    xyz = np.array([[i, j, 0.0] for j in range(n + 1) for i in range(n + 1)], dtype=float)
    inner = np.array([0 < i < n and 0 < j < n for j in range(n + 1) for i in range(n + 1)])
    xyz[inner, :2] += rng.uniform(-0.42, 0.42, size=(int(inner.sum()), 2))
    cells = [[j * (n + 1) + i, j * (n + 1) + i + 1, (j + 1) * (n + 1) + i + 1, (j + 1) * (n + 1) + i]
             for j in range(n) for i in range(n)]
    Q = np.array([c for k, c in enumerate(cells) if k % 3], dtype=np.int64)
    T = np.array([t for k, c in enumerate(cells) if not k % 3 for t in ([c[0], c[1], c[2]], [c[0], c[2], c[3]])],
                 dtype=np.int64)
    kind = np.where(inner, IN, VERTEX).astype(np.int8)
    before = _cost(xyz, Q, T)
    assert before > 0
    Q2, T2, qf, tf, done = sq.mend(xyz, Q, T, [0] * len(Q), [0] * len(T), kind, np.full(len(xyz), -1), {},
                                   limits=AIMS)
    assert done["merged"] + done["recut"] > 0 and _cost(xyz, Q2, T2) < before
    assert (sq.quad_measures(xyz, Q2)["min_jacobian"] > 0).all() and (sq.tria_measures(xyz, T2)["min_jacobian"] > 0).all()
    assert abs(_area(xyz, Q2, T2) - n * n) < 1e-9 and len(qf) == len(Q2) and len(tf) == len(T2)
    assert set(np.flatnonzero(~inner).tolist()) <= set(Q2.ravel().tolist()) | set(T2.ravel().tolist())
    # every element still turns the way it did (z up)
    assert (np.cross(xyz[Q2[:, 2]] - xyz[Q2[:, 0]], xyz[Q2[:, 3]] - xyz[Q2[:, 1]])[:, 2] > 0).all()
    assert (np.cross(xyz[T2[:, 1]] - xyz[T2[:, 0]], xyz[T2[:, 2]] - xyz[T2[:, 0]])[:, 2] > 0).all()
    # the same mesh twice: the same answer
    Q3, T3, _qf, _tf, done3 = sq.mend(xyz, Q, T, [0] * len(Q), [0] * len(T), kind, np.full(len(xyz), -1), {},
                                      limits=AIMS)
    assert Q3.tolist() == Q2.tolist() and T3.tolist() == T2.tolist() and done3 == done
    # asked for quads: no more triangles than there were
    _Q4, T4, _qf, _tf, _d = sq.mend(xyz, Q, T, [0] * len(Q), [0] * len(T), kind, np.full(len(xyz), -1), {},
                                    limits=AIMS, triangles_too=False)
    assert len(T4) <= len(T)


def test_an_edge_over_the_users_longest_counts_against_a_merge():
    # a triangle of 25 degrees (in the band: cost 1) on a 0.45 mm piece of a straight edge; merged away, the quad
    # beside it has a 1.45 mm edge
    xyz, Q, T, kind, edge, ends = _strip(kinds=(EDGE, EDGE), gap=0.45)
    assert _cost(xyz, Q, T) == sq.AIM_BAND
    _Q2, T2, _qf, _tf, done = sq.mend(xyz, Q, T, [0, 0], [0], kind, edge, ends, limits=AIMS)
    assert done["merged"] == 1 and len(T2) == 0
    # with a longest edge of 1.3 mm asked, that only trades one line for another: not made
    Q3, T3, _qf, _tf, done3 = sq.mend(xyz, Q, T, [0, 0], [0], kind, edge, ends, limits=AIMS, edges=(None, 1.3))
    assert done3 == {"merged": 0, "recut": 0} and T3.tolist() == T.tolist() and Q3.tolist() == Q.tolist()
    # an edge outside the user's sizes costs as an element in a band does
    cq, ct, _a, _b = sq._costs(xyz, Q, T, AIMS, (0.5, 1.02))
    assert cq.tolist() == [sq.AIM_BAND, sq.AIM_BAND] and ct.tolist() == [2 * sq.AIM_BAND]


def test_the_size_beside_a_small_piece_of_the_cad():
    # an element may be 2.5 times as long as the piece: a 1 mm edge in 10 mm faces is graded from 2.5 mm
    assert sp.feature_size(1.0, size=10.0, wall=1.0) == 2.5
    # a piece at least 1 / 2.5 of the size needs nothing
    assert sp.feature_size(4.0, size=10.0, wall=1.0) is None and sp.feature_size(5.0, size=10.0, wall=1.0) is None
    # never under the floor (half the wall): a 0.1 mm edge of a 2 mm wall is graded from 1 mm
    assert sp.feature_size(0.1, size=10.0, wall=2.0) == 1.0
    # ... and nothing when the floor is the size already
    assert sp.feature_size(0.1, size=1.0, wall=2.0) is None
    # a narrow face: twice its own width
    assert sp.feature_size(1.0, size=10.0, wall=1.0, ratio=sp.FEATURE_FACE_RATIO) == 2.0
    assert sp.feature_size(0.0, size=10.0, wall=1.0) is None
