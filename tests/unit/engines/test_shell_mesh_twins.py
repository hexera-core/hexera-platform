# Responsibility: Hold the shell clean-up to SHMESH-80 and SHMESH-81: no step writes the same element twice (two
# elements on the same corners), what gmsh writes twice is left once or not at all, and the product's own validity
# counts elements written twice and elements with no area, as a deck reader does.
# Boundaries: numpy on small meshes; no gmsh. (The cap and the triangle re-cut are held on the bolt-hole branch.)
from __future__ import annotations

import numpy as np

from meshpipeline.engines import shell_mesh as sm
from meshpipeline.engines import shell_quality as sq
from meshpipeline.engines.shell_mesh import ShellMesh


def _twins(conn: np.ndarray) -> int:
    if not len(conn):
        return 0
    s = np.sort(conn, axis=1)
    return int(len(s) - len(np.unique(s, axis=0)))


def _mesh(xyz, quads, trias) -> ShellMesh:
    n = len(quads) + len(trias)
    return ShellMesh(xyz=np.asarray(xyz, dtype=float), quads=np.asarray(quads, dtype=np.int64).reshape(-1, 4),
                     trias=np.asarray(trias, dtype=np.int64).reshape(-1, 3), wall=np.ones(n), mid=["Mid_0"] * n,
                     part=["Part_0"] * n)


def test_validity_counts_an_element_written_twice():
    xyz = [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [2, 0, 0]]
    q = sq.measure(_mesh(xyz, [[0, 1, 2, 3]], [[1, 4, 2], [2, 1, 4]]))
    assert q["duplicates"] == 1 and q["invalid"] == 1 and not q["valid"]
    assert "written twice" in q["notes"][0]


def test_validity_counts_an_element_with_no_area():
    # a triangle whose corners lie on one line to 1e-9 mm on a 1 m part: positive corner Jacobian, no area
    xyz = [[0, 0, 0], [1000, 0, 0], [500, 1e-9, 0], [0, 1000, 0]]
    q = sq.measure(_mesh(xyz, [], [[0, 1, 2], [0, 1, 3]]))
    assert q["zero_area"] == 1 and q["folded"] == 0 and not q["valid"]


def test_a_sound_mesh_stays_valid():
    xyz = [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [2, 0, 0]]
    q = sq.measure(_mesh(xyz, [[0, 1, 2, 3]], [[1, 4, 2]]))
    assert q["valid"] and q["invalid"] == 0 and q["duplicates"] == 0 and q["zero_area"] == 0


def test_two_faces_folded_onto_each_other_close_up():
    # faces 0 and 1 each hold the same chord triangle (0, 1, 2) on their shared edges 0-1 and 0-2 (gmsh writes it in
    # both); beside it each has its own triangle across 1-2. Left once, 0-1 and 0-2 would be free and 1-2 held by
    # three: both copies go and the faces meet along 1-2
    quads = np.zeros((0, 4), dtype=np.int64)
    trias = np.array([[0, 1, 2], [0, 2, 1], [1, 3, 2], [2, 4, 1]], dtype=np.int64)
    Q, T, _qf, tf, gone = sq.drop_twins(quads, trias, [], [0, 1, 0, 1])
    assert gone == 2 and T.tolist() == [[1, 3, 2], [2, 4, 1]] and tf == [0, 1]


def test_a_copy_written_in_place_goes_once():
    # a quad with a neighbour on every side, written twice: one copy stays
    xyz, quads = _grid(3, 3)
    dup = np.vstack([quads, quads[4][None]])
    Q, _T, qf, _tf, gone = sq.drop_twins(dup, np.zeros((0, 3), dtype=np.int64), list(range(10)), [])
    assert gone == 1 and len(Q) == 9 and _twins(Q) == 0 and qf == list(range(9))


def _grid(nx: int, ny: int) -> tuple[np.ndarray, np.ndarray]:
    xyz = np.array([[i, j, 0.0] for j in range(ny + 1) for i in range(nx + 1)], dtype=float)
    quads = np.array([[j * (nx + 1) + i, j * (nx + 1) + i + 1, (j + 1) * (nx + 1) + i + 1, (j + 1) * (nx + 1) + i]
                      for j in range(ny) for i in range(nx)], dtype=np.int64)
    return xyz, quads


def test_a_sliver_merge_never_makes_two_elements_one():
    # faces 0 and 1 lie on each other: triangle (0, 1, 2) of face 0 and (0, 3, 2) of face 1, nodes 1 and 3 a sliver
    # apart, joined by a sliver triangle (1, 3, 2) of face 1. Merging 1-3 would make the two one element
    xyz = np.array([[0, 0, 0], [5, 5, 0], [10, 0, 0], [5, 5.001, 0], [5, -5, 0], [5, 10, 0]], dtype=float)
    trias = np.array([[0, 1, 2], [0, 3, 2], [1, 3, 2], [0, 2, 4], [0, 5, 3]], dtype=np.int64)
    faces = [0, 1, 1, 0, 1]
    limit = np.array([1.0, 1.0])
    old, old_drop = sm.TWIN_GUARD, sm.TWIN_DROP
    q0 = np.zeros((0, 4), dtype=np.int64)
    try:
        sm.TWIN_GUARD = False
        _Q, T0, _a, _b, merged0 = sm.collapse_short(xyz, q0, trias, [], faces, limit)
        sm.TWIN_GUARD, sm.TWIN_DROP = True, False
        _Q, T1, _a, _b, merged1 = sm.collapse_short(xyz, q0, trias, [], faces, limit)
        # SHMESH-80b: the merge made and its twins taken out, the surface no worse (free edges, edges held by 3+)
        sm.TWIN_DROP = True
        _Q, T2, _a, _b, merged2 = sm.collapse_short(xyz, q0, trias, [], faces, limit)
    finally:
        sm.TWIN_GUARD, sm.TWIN_DROP = old, old_drop
    assert merged0 == 1 and _twins(T0) == 1
    assert merged1 == 0 and _twins(T1) == 0 and len(T1) == 5
    assert merged2 == 1 and _twins(T2) == 0
    f0, o0 = sm._edge_load(q0, trias)
    f2, o2 = sm._edge_load(q0, T2)
    assert f2 <= f0 and o2 <= o0


def test_a_swap_never_makes_an_edge_that_is_there():
    # triangles 0 (0, 1, 2) and 1 (1, 0, 3) across 0-1; the other diagonal 2-3 is already an edge of triangle 2: the
    # swap would write (0, 3, 2) on top of it
    xyz = np.array([[0, 0, 0], [10, 0, 0], [5, 0.2, 0], [5, -0.2, 0]], dtype=float)
    trias = np.array([[0, 1, 2], [1, 0, 3], [0, 3, 2]], dtype=np.int64)
    T, swaps = sq.flip_past_trias(xyz, trias, [0, 0, 0])
    assert swaps == 0 and _twins(T) == 0
