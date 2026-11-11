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


def _mend(*args, **kw):
    """shell_quality.mend without moves: (quads, triangles, their faces, what was done); no node may have moved."""
    xyz, Q, T, qf, tf, done = sq.mend(*args, **kw)
    assert done.pop("moved") == 0 and (xyz == args[0]).all()
    return Q, T, qf, tf, done


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
    Q2, T2, qf, tf, done = _mend(xyz, Q, T, [0] * 4, [0] * 2, kind, edge, ends, limits=AIMS)
    assert done == {"merged": 1, "recut": 0} and len(T2) == 0 and len(Q2) == 4 and qf == [0] * 4 and tf == []
    assert _cost(xyz, Q2, T2) == 0 and abs(_area(xyz, Q2, T2) - 4.0) < 1e-12
    # every node on the outline is still a corner of the mesh; one of the two inside nodes is gone
    used = set(Q2.ravel().tolist())
    assert set(range(9)) - {4} <= used and len({4, 9} & used) == 1


def test_with_no_user_limit_and_nothing_past_a_solver_limit_nothing_changes():
    xyz, Q, T, kind, edge, ends = _needle_inside()
    Q2, T2, qf, tf, done = _mend(xyz, Q, T, [0] * 4, [0] * 2, kind, edge, ends)
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
    Q2, T2, _qf, _tf, done = _mend(xyz, Q, T, [0, 0], [0], kind, edge, ends, limits=AIMS)
    assert done["merged"] == 0 and {1, 2} <= set(Q2.ravel().tolist()) | set(T2.ravel().tolist())
    assert abs(_area(xyz, Q2, T2) - _area(xyz, Q, T)) < 1e-12
    # "ignore edges under 0.2 mm": the 0.1 mm edge's ends become one node, and the needle is gone
    Q3, T3, _qf, _tf, done3 = _mend(xyz, Q, T, [0, 0], [0], kind, edge, ends, limits=AIMS, suppress=0.2)
    assert done3["merged"] == 1 and len(T3) == 0 and len(Q3) == 2 and _cost(xyz, Q3, T3) == 0
    # ... but not an edge as long as the length they gave
    _Q4, T4, _qf, _tf, done4 = _mend(xyz, Q, T, [0, 0], [0], kind, edge, ends, limits=AIMS, suppress=0.1)
    assert done4["merged"] == 0 and len(T4) == 1


def test_a_node_on_a_cad_edge_goes_only_along_a_straight_piece_of_it():
    xyz, Q, T, kind, edge, ends = _strip(kinds=(EDGE, EDGE))
    Q2, T2, _qf, _tf, done = _mend(xyz, Q, T, [0, 0], [0], kind, edge, ends, limits=AIMS)
    assert done["merged"] == 1 and len(T2) == 0 and _cost(xyz, Q2, T2) == 0
    assert abs(_area(xyz, Q2, T2) - _area(xyz, Q, T)) < 1e-12              # the edge is where it was
    # the same nodes on a bent edge (node 2 stands 0.05 off the line): merging either would move the edge
    xyz, Q, T, kind, edge, ends = _strip(drop=0.05, kinds=(EDGE, EDGE))
    _Q3, T3, _qf, _tf, done3 = _mend(xyz, Q, T, [0, 0], [0], kind, edge, ends, limits=AIMS)
    assert done3["merged"] == 0 and len(T3) == 1


def test_two_triangles_are_cut_along_the_other_diagonal_when_that_mends_them():
    # a kite cut along its long diagonal: two triangles with a corner of 152 degrees; along the short one: 76 at most
    xyz = np.array([[0, 0, 0], [2, -0.5, 0], [4, 0, 0], [2, 0.5, 0]], dtype=float)
    T = np.array([[0, 1, 2], [0, 2, 3]], dtype=np.int64)
    kind = np.full(4, VERTEX, dtype=np.int8)
    limits = sq.InForce(aims=(("tria", "max_angle_tria", "<=", 120.0, 140.0, 0.01),))
    Q0 = np.zeros((0, 4), dtype=np.int64)
    assert _cost(xyz, Q0, T, limits) == 2 * sq.AIM_OUTER
    Q2, T2, _qf, tf, done = _mend(xyz, Q0, T, [], [0, 0], kind, np.full(4, -1), {}, limits=limits,
                                    quads_too=False)
    assert done == {"merged": 0, "recut": 1} and len(Q2) == 0 and len(T2) == 2 and tf == [0, 0]
    assert _cost(xyz, Q2, T2, limits) == 0 and abs(_area(xyz, Q2, T2) - 2.0) < 1e-12
    # never across two faces
    _Q3, T3, _qf, _tf, done3 = _mend(xyz, Q0, T, [], [0, 1], kind, np.full(4, -1), {}, limits=limits)
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
    Q2, T2, qf, tf, done = _mend(xyz, Q, T, [0] * len(Q), [0] * len(T), kind, np.full(len(xyz), -1), {},
                                   limits=AIMS)
    assert done["merged"] + done["recut"] > 0 and _cost(xyz, Q2, T2) < before
    assert (sq.quad_measures(xyz, Q2)["min_jacobian"] > 0).all() and (sq.tria_measures(xyz, T2)["min_jacobian"] > 0).all()
    assert abs(_area(xyz, Q2, T2) - n * n) < 1e-9 and len(qf) == len(Q2) and len(tf) == len(T2)
    assert set(np.flatnonzero(~inner).tolist()) <= set(Q2.ravel().tolist()) | set(T2.ravel().tolist())
    # every element still turns the way it did (z up)
    assert (np.cross(xyz[Q2[:, 2]] - xyz[Q2[:, 0]], xyz[Q2[:, 3]] - xyz[Q2[:, 1]])[:, 2] > 0).all()
    assert (np.cross(xyz[T2[:, 1]] - xyz[T2[:, 0]], xyz[T2[:, 2]] - xyz[T2[:, 0]])[:, 2] > 0).all()
    # SHMESH-28b: under every line the worst element is no further past it than before (or than the line's band)
    held_before: list = []
    sq._costs(xyz, Q, T, AIMS, bad=held_before)
    held_after: list = []
    sq._costs(xyz, Q2, T2, AIMS, bad=held_after)
    band = np.array([abs(o - t) for _s, _k, _op, t, o, _sh in AIMS.aims] + [0.0] * len(AIMS.hard))
    assert np.all(np.vstack(held_after).max(axis=0) <= np.maximum(np.vstack(held_before).max(axis=0), band) + 1e-9)
    # the same mesh twice: the same answer
    Q3, T3, _qf, _tf, done3 = _mend(xyz, Q, T, [0] * len(Q), [0] * len(T), kind, np.full(len(xyz), -1), {},
                                      limits=AIMS)
    assert Q3.tolist() == Q2.tolist() and T3.tolist() == T2.tolist() and done3 == done
    # asked for quads: no more triangles than there were
    _Q4, T4, _qf, _tf, _d = _mend(xyz, Q, T, [0] * len(Q), [0] * len(T), kind, np.full(len(xyz), -1), {},
                                    limits=AIMS, triangles_too=False)
    assert len(T4) <= len(T)


def test_an_edge_over_the_users_longest_counts_against_a_merge():
    # a triangle of 25 degrees (in the band: cost 1) on a 0.45 mm piece of a straight edge; merged away, the quad
    # beside it has a 1.45 mm edge
    xyz, Q, T, kind, edge, ends = _strip(kinds=(EDGE, EDGE), gap=0.45)
    assert _cost(xyz, Q, T) == sq.AIM_BAND
    _Q2, T2, _qf, _tf, done = _mend(xyz, Q, T, [0, 0], [0], kind, edge, ends, limits=AIMS)
    assert done["merged"] == 1 and len(T2) == 0
    # with a longest edge of 1.3 mm asked, that only trades one line for another: not made
    Q3, T3, _qf, _tf, done3 = _mend(xyz, Q, T, [0, 0], [0], kind, edge, ends, limits=AIMS, edges=(None, 1.3))
    assert done3 == {"merged": 0, "recut": 0} and T3.tolist() == T.tolist() and Q3.tolist() == Q.tolist()
    # an edge outside the user's sizes costs as an element in a band does
    cq, ct, _a, _b = sq._costs(xyz, Q, T, AIMS, (0.5, 1.02))
    # (the triangle: in the band, an edge under 0.5 mm, an edge over 1.02 mm; each quad: an edge over 1.02 mm)
    assert cq.tolist() == [sq.AIM_BAND, sq.AIM_BAND] and ct.tolist() == [3 * sq.AIM_BAND]


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


def test_a_sheet_drawn_as_patches_is_one_region():
    # faces 0-1-2 joined smoothly, 3 apart, 4 joined to 3
    assert sp.sheet_regions(5, [(1, 2), (0, 1), (3, 4)]) == [0, 0, 0, 3, 3]
    assert sp.sheet_regions(3, []) == [0, 1, 2]
    wall = 2.0
    # flat patches meeting flush: one region; a crease of 10 degrees: not
    assert sp.smooth_link(0.5, same_wall=True, bends=(None, None), wall=wall)
    assert not sp.smooth_link(10.0, same_wall=True, bends=(None, None), wall=wall)
    assert not sp.smooth_link(0.0, same_wall=False, bends=(None, None), wall=wall)
    # a gently curved panel (radius 400 mm = 200 walls) and a flat patch: one region
    assert sp.smooth_link(1.0, same_wall=True, bends=(400.0, None), wall=wall)
    # a bend (radius 4 mm = 2 walls) keeps the flange beside it apart
    assert not sp.smooth_link(0.0, same_wall=True, bends=(4.0, None), wall=wall)
    # ... but a bend drawn as two patches of one radius is one bend; two bends of other radii are not
    assert sp.smooth_link(0.0, same_wall=True, bends=(4.0, 4.1), wall=wall)
    assert not sp.smooth_link(0.0, same_wall=True, bends=(4.0, 6.0), wall=wall)


def test_the_quick_angle_check_never_turns_down_a_quad_the_full_measure_keeps():
    rng = np.random.default_rng(3)
    bounds = sq._angle_bounds(sq.IN_FORCE)
    assert bounds == (30.0, 150.0)
    base = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], dtype=float)
    quick = 0
    for _ in range(3000):
        p = base + rng.normal(scale=0.35, size=(4, 3)) * np.array([1, 1, 0.2])
        full = bool(sq._quad_past(sq.quad_measures(p, np.array([[0, 1, 2, 3]])))[0])
        if sq._clearly_past(p.tolist(), bounds):
            quick += 1
            assert full
        # and the quad walks read the same answer with the quick check as without it
        n = np.array([0.0, 0.0, 1.0])
        got = sq._good_quad(p, [0, 1, 2, 3], n)
        m = sq.quad_measures(p, np.array([[0, 1, 2, 3]]))
        want = None
        if not full and float(np.dot(np.cross(p[2] - p[0], p[3] - p[1]), n)) > 0:
            want = float(m["min_angle_quad"][0])
        assert got == want
    assert quick > 300                                        # the quick check does turn many down


def test_a_bead_wall_keeps_two_elements_across_never_under_the_floor():
    # a 10.9 mm wall at a 10 mm size: 5.45 mm (2 across); a 1 mm wall of a 1.2 mm sheet: the floor, 0.6 mm
    assert sp.flank_size(10.9, size=10.0, wall=1.2) == 5.45
    assert sp.flank_size(1.0, size=10.0, wall=1.2) == 0.6
    # the size already gives 2 across: nothing
    assert sp.flank_size(25.0, size=10.0, wall=1.2) is None


def test_holes_and_curves_under_the_users_size():
    from meshpipeline.engines import shell_requirements as SR

    # a 6 mm radius hole at 10 mm: 8 edges round it (not 4: a square)
    h = SR.curved_size(6.0, 0.0, floor=6.5, rule=10.0, per_turn=SR.HOLE_PER_TURN)
    assert abs(h - 2 * np.pi * 6.0 / 8) < 1e-9
    # a large hole takes the size
    assert SR.curved_size(60.0, 0.0, floor=6.5, rule=10.0, per_turn=SR.HOLE_PER_TURN) == 10.0
    # a gently curved panel (200 mm radius) takes the user's 10 mm: its middle stands 0.06 mm off the arc
    assert SR.curved_size(200.0, 300.0, floor=6.5, rule=10.0, chord=1.0) == 10.0
    # a 4 mm radius at 10 mm is held so an element's middle stands at most 1 mm (a tenth of the size) off it
    h = SR.curved_size(4.0, 50.0, floor=0.5, rule=10.0, chord=1.0)
    assert abs(4.0 * (1 - np.cos(h / 8.0)) - 1.0) < 1e-9 and h < 2 * np.pi * 4.0 / 4
    # a narrow bend keeps one element across unless the chord holds it
    assert SR.curved_size(20.0, 30.0, floor=0.5, rule=10.0, chord=1.0) == 10.0
    assert SR.chord_size(5.0, 6.0) == float("inf")


def test_the_earlier_steps_mesh_is_set_against_a_mesh_past_a_limit():
    from meshpipeline.engines import shell_requirements as SR
    from meshpipeline.engines.gmsh import shell_adapter as SA

    def applied(beyond, met):
        rows = [{"field": "limit:min_angle", "beyond": b, "met": m} for b, m in zip(beyond, met, strict=True)]
        return {"requirements": {"rows": rows}}

    # fewer past the solver first, then fewer past an outer limit, then more lines met
    assert SA._standing(applied([0, 0], [True, True]), 1) > SA._standing(applied([5, 5], [False, False]), 0)
    assert SA._standing(applied([2, 0], [False, True]), 0) > SA._standing(applied([0, 0], [False, False]), 0)
    assert SA._standing(applied([0, 0], [True, False]), 0) > SA._standing(applied([0, 0], [True, True]), 0)
    # the earlier steps alone, for one mesh; every constant put back after
    before = (SA.FEATURE_SIZES, SA.MEND, SA.SLIDE_EDGES, SR.HOLE_PER_TURN, SR.CHORD_SHARE)
    with SA._classic():
        assert (SA.FEATURE_SIZES, SA.MEND, SA.SLIDE_EDGES) == (False, False, False)
        assert SR.HOLE_PER_TURN == SR.SHAPE_PER_TURN
    assert (SA.FEATURE_SIZES, SA.MEND, SA.SLIDE_EDGES, SR.HOLE_PER_TURN, SR.CHORD_SHARE) == before
