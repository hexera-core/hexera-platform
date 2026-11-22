# Responsibility: Hold SHMESH-83: a quad the cutting steps would cut into two triangles is first repaired - a free
# corner moved, or it and the quad beside it cut again the other way - only where the quad comes out clean and no
# line gets worse round it; pinned nodes (rings, bands, grid cells) never move or re-cut.
# Boundaries: numpy on small made-up meshes; no gmsh.
from __future__ import annotations

import numpy as np
import pytest

from meshpipeline.engines import shell_quality as sq
from meshpipeline.engines import shell_repair as R

LIM = sq.InForce(aims=(("quad", "min_angle_quad", ">=", 45.0, 30.0, 0.01),
                       ("quad", "max_angle_quad", "<=", 135.0, 150.0, 0.01)))


def _grid(n: int) -> tuple[np.ndarray, np.ndarray]:
    xyz = np.array([[i, j, 0.0] for j in range(n + 1) for i in range(n + 1)], dtype=float)
    q = np.array([[j * (n + 1) + i, j * (n + 1) + i + 1, (j + 1) * (n + 1) + i + 1, (j + 1) * (n + 1) + i]
                  for j in range(n) for i in range(n)], dtype=np.int64)
    return xyz, q


def _repair(xyz, q, movable, pinned, stage="past", lim=LIM):
    return R.repair_quads(xyz, q, np.zeros((0, 3), np.int64), [0] * len(q), [], movable, pinned, R.Lines(lim),
                          stage=stage)


def test_a_quad_past_a_limit_is_moved_back_not_cut():
    xyz, q = _grid(2)
    xyz[4, :2] = [1.85, 1.8]                       # the middle node pulled into a corner: its quads past Nastran
    assert sq._quad_past(sq.quad_measures(xyz, q), LIM).any()
    mov = np.zeros(len(xyz), dtype=bool)
    mov[4] = True
    X, Q, T, _qf, _tf, done = _repair(xyz, q, mov, np.zeros(len(xyz), dtype=bool))
    assert done["moved"] >= 1 and len(T) == 0 and len(Q) == 4
    assert not R.unclean(X, Q, LIM).any()
    # the rule: no line's count rose
    assert not (sq.line_matrix(X, Q, LIM).sum(axis=0) > sq.line_matrix(xyz, q, LIM).sum(axis=0)).any()


def test_a_pinned_node_never_moves():
    xyz, q = _grid(2)
    xyz[4, :2] = [1.85, 1.8]
    mov = np.zeros(len(xyz), dtype=bool)
    mov[4] = True
    pinned = np.zeros(len(xyz), dtype=bool)
    pinned[4] = True
    X, Q, _T, _qf, _tf, done = _repair(xyz, q, mov, pinned)
    assert done == {"moved": 0, "repaired": 0} and np.array_equal(X, xyz) and np.array_equal(Q, q)


def test_two_quads_cut_again_the_other_way():
    # a six-sided outline cut into two quads along 1-4, one past the user's angle targets; cut along 0-3 both are clean
    xyz = np.array([[0.957, 0.27, 0], [0.594, 0.883, 0], [-0.387, 1.017, 0], [-0.802, 0.066, 0], [-0.45, -1.03, 0],
                    [0.276, -0.934, 0]], dtype=float)
    q = np.array([[1, 2, 3, 4], [4, 5, 0, 1]], dtype=np.int64)
    assert R.unclean(xyz, q, LIM).any()
    X, Q, T, _qf, _tf, done = _repair(xyz, q, np.zeros(6, dtype=bool), np.zeros(6, dtype=bool), stage="aimed")
    assert done["repaired"] == 1 and len(T) == 0 and np.array_equal(X, xyz)
    assert not R.unclean(X, Q, LIM).any()
    assert sorted(map(sorted, Q.tolist())) == [[0, 1, 2, 3], [0, 3, 4, 5]]


def test_pinned_nodes_cover_rings_bands_and_grid_cells():
    q = np.array([[0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 10, 11]], dtype=np.int64)
    pinned = R.pinned_nodes(12, q, [0, 1, 2], made_from=10, band_nodes={4}, grid_faces={1})
    assert pinned.tolist() == [False] * 4 + [True] * 4 + [True] * 4


def test_in_band_quads_are_cut_together_over_every_line():
    # SHMESH-86: quad 0 is in both lines' bands, 1 only in the first, 2 only in the second; each line allows one. Cut
    # line by line the furthest of each (1 and 2) go; together quad 0 alone brings both lines within their allowance
    xyz, q = _grid(3)
    q = q[:3]
    band1 = np.array([True, True, False])
    band2 = np.array([True, False, True])
    far1 = np.array([0.1, 0.9, 0.0])
    far2 = np.array([0.1, 0.0, 0.9])
    lim = sq.InForce(aims=(("quad", "min_angle_quad", ">=", 45.0, 30.0, 0.01),))
    pick = sq._cover(xyz, q, lim, np.zeros(3, dtype=bool), [(band1, 1, far1), (band2, 1, far2)])
    assert pick.tolist() == [True, False, False]


def test_a_line_within_its_allowance_asks_nothing_of_the_mend():
    # 100 squares and one rhombus of 40 degrees (in the 45/30 band): 1 in band against 1 allowed - the line reads at
    # the inner edge of its outer tenth (SHMESH-86c: 31.5; at its outer limit with NEAR_MEND off); two rhombi against
    # one allowed - it stays
    xyz, q = _grid(10)
    lim = sq.InForce(aims=(("quad", "min_angle_quad", ">=", 45.0, 30.0, 0.01),))
    xyz2 = xyz.copy()
    xyz2[[1, 12], 0] += 0.84                     # quad 0 leans: corners near 40 degrees
    m = sq.quad_measures(xyz2, q)
    inband = int(((m["min_angle_quad"] < 45) & (m["min_angle_quad"] >= 30)).sum())
    got = sq.within_allowance(xyz2, q, np.zeros((0, 3), np.int64), lim)
    within = 30.0 + 15.0 * sq.NEAR_OUTER
    if inband <= 1:
        assert got.aims[0][3] == pytest.approx(within)
    else:
        assert got.aims[0][3] == 45.0
    assert sq.within_allowance(xyz, q, np.zeros((0, 3), np.int64), lim).aims[0][3] == pytest.approx(within)


def test_protected_faces_ride_with_the_limits_and_bands_are_counted_apart():
    lim = sq.with_protect(sq.with_edges(LIM, (0.9, 0.8, None, None)), {3, 7}, [1, 2])
    assert lim.protect == frozenset({3, 7}) and lim.protect_nodes == frozenset({1, 2})
    assert sq.with_edges(lim, (1.0, 0.9, None, None)).protect == lim.protect
    xyz, q = _grid(2)
    xyz2 = xyz.copy()
    xyz2[4, 0] += 0.35                              # the middle node pulled aside: two quads leave the 45 deg target
    band = sq._band_rows(xyz2, q, LIM)
    m = sq.quad_measures(xyz2, q)
    want = ((m["min_angle_quad"] < 45) & (m["min_angle_quad"] >= 30)) | ((m["max_angle_quad"] > 135)
                                                                        & (m["max_angle_quad"] <= 150))
    assert band.tolist() == want.astype(int).tolist()
