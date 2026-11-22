# Responsibility: Hold the short-edge mend to its gate (SHMESH-28e): only our own elements under the user's shortest
# edge are mended - an edge the part forces (across two CAD edges, along a CAD edge shorter than the shortest edge or
# a hole too small for it, the two ends of one CAD edge) is left; a change is kept only where, face by face, no count
# gets worse (the user's lines, the solver's, the edge lines, the triangles) and the elements under the shortest edge
# fall; no merge or re-cut on a protected (structured) face.
# Boundaries: numpy on small made-up meshes; no gmsh, no solver.
from __future__ import annotations

import numpy as np

from meshpipeline.engines import shell_quality as sq

AIMS = sq.InForce(aims=(("tria", "min_angle_tria", ">=", 30.0, 20.0, 0.01),
                        ("tria", "max_angle_tria", "<=", 120.0, 140.0, 0.01),
                        ("quad", "min_angle_quad", ">=", 45.0, 30.0, 0.01),
                        ("quad", "max_angle_quad", "<=", 135.0, 150.0, 0.01)))
IN, EDGE, VERTEX = sq.INSIDE, sq.ON_EDGE, sq.AT_VERTEX


def _lim(low, high=None):
    return sq.with_edges(AIMS, (low, low * 0.8, high, None if high is None else high * 1.2))


def _strip(kinds=(EDGE, EDGE), gap: float = 0.5):
    """Two quads along a free edge with a triangle between them: bottom nodes 0, 1, 2, 3 (1 and 2 `gap` apart), top
    nodes 4, 5, 6."""
    xyz = np.array([[0, 0, 0], [1, 0, 0], [1 + gap, 0, 0], [2 + gap, 0, 0], [0, 1, 0], [1 + gap / 2, 1, 0],
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
    return xyz, Q, T, kind, edge, ends


def _gated(xyz, Q, T, kind, edge, ends, limits, gate, qf=None, tf=None):
    return sq.mend(xyz, Q, T, qf or [0] * len(Q), tf or [0] * len(T), kind, edge, ends, limits=limits,
                   edges=(gate.low, None), short_gate=gate)


def test_an_edge_of_ours_under_the_shortest_edge_is_mended():
    xyz, Q, T, kind, edge, ends = _strip(gap=0.5)
    gate = sq.ShortGate(low=0.6)
    _x, Q2, T2, _qf, _tf, done = _gated(xyz, Q, T, kind, edge, ends, _lim(0.6), gate)
    assert done["merged"] == 1 and len(T2) == 0 and len(Q2) == 2


def test_the_parts_own_short_edges_are_left():
    # the two ends of a 0.5 mm CAD edge (no node between them): forced, left
    xyz, Q, T, kind, edge, ends = _strip(kinds=(VERTEX, VERTEX), gap=0.5)
    gate = sq.ShortGate(low=0.6)
    assert not sq.short_ours(xyz, Q, T, kind, edge, ends, gate).any()
    _x, Q2, T2, _qf, _tf, done = _gated(xyz, Q, T, kind, edge, ends, _lim(0.6), gate)
    assert done == {"merged": 0, "recut": 0, "moved": 0} and T2.tolist() == T.tolist()
    # along a CAD edge the record calls forcing (a hole too small for 8 of the shortest edge a turn): left
    xyz, Q, T, kind, edge, ends = _strip(gap=0.5)
    gate = sq.ShortGate(low=0.6, forced_curves=frozenset({10}))
    _x, _Q2, T2, _qf, _tf, done = _gated(xyz, Q, T, kind, edge, ends, _lim(0.6), gate)
    assert done["merged"] == 0 and len(T2) == 1
    # an edge across two CAD edges that share none (a gap narrower than the shortest edge): forced
    assert sq.edge_forced(1, 5, kind, edge, ends, frozenset())
    # an edge with an end inside a face: ours
    kind2 = kind.copy()
    kind2[5] = IN
    assert not sq.edge_forced(1, 5, kind2, edge, ends, frozenset())


def test_no_change_that_makes_another_line_worse():
    # merged away, the 0.45 mm piece leaves a 1.45 mm edge: over a longest edge of 1.3 mm - refused
    xyz, Q, T, kind, edge, ends = _strip(gap=0.45)
    gate = sq.ShortGate(low=0.6)
    _x, Q2, T2, _qf, _tf, done = _gated(xyz, Q, T, kind, edge, ends, _lim(0.6, 1.3), gate)
    assert done["merged"] == 0 and T2.tolist() == T.tolist() and Q2.tolist() == Q.tolist()


def test_no_merge_on_a_structured_face():
    xyz, Q, T, kind, edge, ends = _strip(gap=0.5)
    gate = sq.ShortGate(low=0.6)
    lim = sq.with_protect(_lim(0.6), [0])
    _x, _Q2, T2, _qf, _tf, done = _gated(xyz, Q, T, kind, edge, ends, lim, gate)
    assert done["merged"] == 0 and done["recut"] == 0 and len(T2) == 1


def _plate(col=0.3):
    """A 3 x 3 quad plate whose first inner column of nodes sits `col` from the left edge (thin quads)."""
    xs = [0.0, 1.0, 2.0, 3.0]
    xyz = np.array([[x, y, 0.0] for y in range(4) for x in xs])
    for j in (1, 2):
        xyz[4 * j + 1, 0] = col
    Q = np.array([[j * 4 + i, j * 4 + i + 1, (j + 1) * 4 + i + 1, (j + 1) * 4 + i] for j in range(3) for i in range(3)])
    kind, edge = [], []
    for x, y, _z in xyz:
        on_x, on_y = x in (0.0, 3.0), y in (0.0, 3.0)
        kind.append(VERTEX if on_x and on_y else EDGE if on_x or on_y else IN)
        edge.append(-1 if (on_x and on_y) or not (on_x or on_y) else 10 if y == 0 else 12 if y == 3 else
                    13 if x == 0 else 11)
    ends = {0: {10, 13}, 3: {10, 11}, 15: {11, 12}, 12: {12, 13}}
    return xyz, Q, np.zeros((0, 3), np.int64), np.array(kind, np.int8), np.array(edge, np.int64), ends


def _counts(xyz, Q, T, lim):
    return np.vstack([sq.line_matrix(xyz, Q, lim), sq.line_matrix(xyz, T, lim)]).sum(axis=0)


def test_quads_are_not_turned_into_triangles_for_a_short_edge_and_no_line_rises():
    xyz, Q, T, kind, edge, ends = _plate(0.3)
    lim = _lim(0.6)
    gate = sq.ShortGate(low=0.6)
    movable = kind == IN
    X2, Q2, T2, _qf, _tf, done = sq.mend(xyz, Q, T, [0] * len(Q), [], kind, edge, ends, limits=lim,
                                         edges=(0.6, None), movable=movable, short_gate=gate)
    before, after = _counts(xyz, Q, T, lim), _counts(X2, Q2, T2, lim)
    assert len(T2) == 0 and done["merged"] == 0
    assert (after <= before).all()
    cols = sq.edge_cols(lim)
    assert after[cols[0]] < before[cols[0]]          # the inner nodes moved off the thin column: fewer under


def test_triangles_may_rise_only_on_faces_the_gate_names():
    xyz, Q, T, kind, edge, ends = _plate(0.3)
    lim = _lim(0.6)
    none = sq.ShortGate(low=0.6, tri_faces=frozenset())
    _x, _Q, T_none, _qf, _tf, _d = sq.mend(xyz, Q, T, [0] * len(Q), [], kind, edge, ends, limits=lim,
                                           edges=(0.6, None), short_gate=none)
    assert len(T_none) == 0


def test_the_local_pass_changes_what_the_whole_mesh_pass_changes_and_nothing_else():
    # a 9 x 9 plate, the thin column at its left only: the part of the mesh two rings round it is mended; every
    # element further out stays as it was, and the mesh is the whole-mesh pass's (as sets of elements)
    xs = np.arange(10, dtype=float)
    xyz = np.array([[x, y, 0.0] for y in range(10) for x in xs])
    for j in range(1, 9):
        xyz[10 * j + 1, 0] = 0.3
    Q = np.array([[j * 10 + i, j * 10 + i + 1, (j + 1) * 10 + i + 1, (j + 1) * 10 + i] for j in range(9) for i in range(9)])
    kind, edge = [], []
    for x, y, _z in xyz:
        on_x, on_y = x in (0.0, 9.0), y in (0.0, 9.0)
        kind.append(VERTEX if on_x and on_y else EDGE if on_x or on_y else IN)
        edge.append(-1 if (on_x and on_y) or not (on_x or on_y) else 10 if y == 0 else 12 if y == 9 else
                    13 if x == 0 else 11)
    kind, edge = np.array(kind, np.int8), np.array(edge, np.int64)
    ends = {0: {10, 13}, 9: {10, 11}, 99: {11, 12}, 90: {12, 13}}
    T = np.zeros((0, 3), np.int64)
    lim = _lim(0.6)
    gate = sq.ShortGate(low=0.6)
    movable = kind == IN
    Xw, Qw, Tw, _a, _b, dw = sq.mend(xyz, Q, T, [0] * len(Q), [], kind, edge, ends, limits=lim, edges=(0.6, None),
                                     movable=movable, short_gate=gate, rounds=1)
    Xl, Ql, Tl, qfl, _c, dl = sq.mend_short_local(xyz, Q, T, [0] * len(Q), [], kind, edge, ends, gate=gate, limits=lim,
                                                  edges=(0.6, None), movable=movable, rounds=1)
    assert dl == dw and dl["moved"] > 0
    assert np.allclose(Xl, Xw) and {tuple(c) for c in Ql.tolist()} == {tuple(c) for c in Qw.tolist()}
    assert len(Tl) == len(Tw) == 0 and len(qfl) == len(Ql)
    far = [k for k, c in enumerate(Q.tolist()) if min(xyz[v][0] for v in c) >= 4.0]
    assert {tuple(Q[k]) for k in far} <= {tuple(c) for c in Ql.tolist()}
    assert np.allclose(Xl[xyz[:, 0] >= 4.0], xyz[xyz[:, 0] >= 4.0])
