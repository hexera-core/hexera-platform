# Responsibility: Prove bound_edges cuts the rims fine and the interior coarse, fans wide thin
#                 triangles and zips needles that touch a rim, keeps the sheet conforming and
#                 oriented, and never emits a degenerate triangle.
from __future__ import annotations

import numpy as np
import pytest

from meshpipeline.engines.vmtk.rims import _halve_long_edges, bound_edges, rim_edges


def _square(side=1.0):
    # a square, two triangles: four rim edges, one interior diagonal
    pts = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], dtype=float) * side
    tri = np.array([[0, 1, 2], [0, 2, 3]])
    return pts, tri


def _areas(pts, tri):
    a, b, c = pts[tri[:, 0]], pts[tri[:, 1]], pts[tri[:, 2]]
    return 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1)


def _rim_length(pts, tri):
    fi, li = rim_edges(tri, len(pts))
    a = tri[fi, li]
    b = tri[fi, (li + 1) % 3]
    return float(np.linalg.norm(pts[b] - pts[a], axis=1).sum()), len(fi)


def _edge_stats(pts, tri):
    e = np.sort(np.concatenate([tri[:, [0, 1]], tri[:, [1, 2]], tri[:, [2, 0]]]), axis=1)
    eu, cnt = np.unique(e, axis=0, return_counts=True)
    return np.linalg.norm(pts[eu[:, 1]] - pts[eu[:, 0]], axis=1), cnt


def _aspects(pts, tri):
    a = np.linalg.norm(pts[tri[:, 1]] - pts[tri[:, 0]], axis=1)
    b = np.linalg.norm(pts[tri[:, 2]] - pts[tri[:, 1]], axis=1)
    c = np.linalg.norm(pts[tri[:, 0]] - pts[tri[:, 2]], axis=1)
    return np.max([a, b, c], axis=0) / np.min([a, b, c], axis=0)


def _sound(pts, tri, *, up=True):
    """conforming, non-degenerate, oriented"""
    _, owners = _edge_stats(pts, tri)
    assert owners.max() <= 2
    assert (_areas(pts, tri) > 1e-12).all()
    a, b, c = pts[tri[:, 0]], pts[tri[:, 1]], pts[tri[:, 2]]
    if up:
        assert (np.cross(b - a, c - a)[:, 2] > 0).all()


def test_rim_edges_finds_only_single_owner_edges():
    pts, tri = _square()
    fi, li = rim_edges(tri, len(pts))
    assert len(fi) == 4
    assert set(zip(fi.tolist(), li.tolist())) == {(0, 0), (0, 1), (1, 1), (1, 2)}


def test_rims_are_cut_fine_and_the_interior_stays_coarse():
    pts, tri = _square(4.0)                                # 4 x 4; interior diagonal 5.66
    p2, t2 = bound_edges(pts, tri, h_wall=10.0, h_rim=1.0)
    _sound(p2, t2)
    total, n_rim = _rim_length(p2, t2)
    assert total == pytest.approx(16.0) and n_rim == 16      # each 4-long rim edge in 4 pieces
    assert _areas(p2, t2).sum() == pytest.approx(16.0)
    assert (p2[:4] == pts).all()
    length, _ = _edge_stats(p2, t2)
    assert length.max() < 5.66                               # spokes are shorter than the edges


def test_every_edge_is_bounded_by_the_wall_length():
    pts, tri = _square(4.0)
    p2, t2 = bound_edges(pts, tri, h_wall=1.0, h_rim=1.0)
    _sound(p2, t2)
    length, _ = _edge_stats(p2, t2)
    assert length.max() <= 1.0 + 1e-9
    assert _rim_length(p2, t2)[0] == pytest.approx(16.0)
    assert _areas(p2, t2).sum() == pytest.approx(16.0)


def test_a_needle_at_a_rim_is_zipped_with_a_tiny_tip_not_left_whole_nor_exploded():
    pts = np.array([[0, 0, 0], [10, 0, 0], [10, 0.1, 0]], dtype=float)   # a 10 x 0.1 needle
    tri = np.array([[0, 1, 2]])
    p2, t2 = bound_edges(pts, tri, h_wall=100.0, h_rim=100.0)
    assert len(t2) == 1                                    # nothing longer than either bound
    p2, t2 = bound_edges(pts, tri, h_wall=100.0, h_rim=1.0)
    _sound(p2, t2)
    fi, li = rim_edges(t2, len(p2))
    rim_len = np.linalg.norm(p2[t2[fi, (li + 1) % 3]] - p2[t2[fi, li]], axis=1)
    assert rim_len.max() <= 1.0 + 1e-9                     # the rim itself is cut to h_rim
    assert 16 <= len(t2) <= 40                             # a ladder of ~2 x 10 rungs, not 3**4
    assert _areas(p2, t2).sum() == pytest.approx(0.5)
    at_tip = (t2 == 0).any(axis=1)                         # the tip vertex sees tiny squat ones
    assert _aspects(p2, t2)[at_tip].max() <= 4.0
    assert _edge_stats(p2, t2[at_tip])[0].max() <= 1.0 + 1e-9


def test_a_wide_thin_triangle_at_a_rim_is_fanned_not_zipped():
    # a 40 x 4 strip meeting its rim on the long side (aspect 10: thin, not a needle): halved to
    # 8 first, then fanned; nothing touching the rim is a long spoke
    pts = np.array([[0, 0, 0], [40, 0, 0], [40, 4, 0], [0, 4, 0]], dtype=float)
    tri = np.array([[0, 1, 2], [0, 2, 3]])
    p2, t2 = bound_edges(pts, tri, h_wall=8.0, h_rim=1.0)
    _sound(p2, t2)
    length, _ = _edge_stats(p2, t2)
    assert length.max() <= 8.0 + 1e-9
    fi, li = rim_edges(t2, len(p2))
    assert _aspects(p2, t2)[np.unique(fi)].max() <= 10.0    # not 27
    assert _areas(p2, t2).sum() == pytest.approx(160.0)
    assert _rim_length(p2, t2)[0] == pytest.approx(88.0)


def test_a_needle_away_from_every_rim_is_left_to_the_wall_bound():
    # a closed box with one face split into two 4 x 0.2 needles: nothing touches a rim, so only
    # h_wall applies
    pts = np.array([[0, 0, 0], [4, 0, 0], [4, 0.2, 0], [0, 0.2, 0],
                    [0, 0, 1], [4, 0, 1], [4, 0.2, 1], [0, 0.2, 1]], dtype=float)
    tri = np.array([[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4],
                    [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]])
    p2, t2 = bound_edges(pts, tri, h_wall=10.0, h_rim=0.1)
    assert len(t2) == 12
    p3, t3 = bound_edges(pts, tri, h_wall=1.0, h_rim=0.1)
    _sound(p3, t3, up=False)
    length, owners = _edge_stats(p3, t3)
    assert length.max() <= 1.0 + 1e-9 and owners.min() == 2


def test_short_triangles_are_left_exactly_as_they_were():
    pts = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [5, 5, 0]], dtype=float)
    tri = np.array([[0, 1, 2], [1, 3, 2]])   # a small triangle sharing an edge with a long one
    p2, t2 = bound_edges(pts, tri, h_wall=2.0, h_rim=2.0)
    assert [0, 1, 2] in t2.tolist()
    _sound(p2, t2)
    assert (p2[:4] == pts).all()
    length, _ = _edge_stats(p2, t2)
    assert length.max() <= 2.0 + 1e-9


def test_nothing_to_cut_is_the_identity():
    pts, tri = _square()
    p2, t2 = bound_edges(pts, tri, h_wall=2.0, h_rim=2.0)
    assert (p2 == pts).all() and (t2 == tri).all()
    p0, t0 = bound_edges(pts[:0], tri[:0], h_wall=1.0, h_rim=1.0)
    assert len(p0) == 0 and len(t0) == 0


def test_a_closed_sheet_has_no_rim_to_cut_but_keeps_its_bounds():
    pts = np.array([[0, 0, 0], [4, 0, 0], [0, 4, 0], [0, 0, 4]], dtype=float)
    tri = np.array([[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]])
    p2, t2 = bound_edges(pts, tri, h_wall=2.0, h_rim=0.1)
    _sound(p2, t2, up=False)
    length, owners = _edge_stats(p2, t2)
    assert length.max() <= 2.0 + 1e-9 and owners.min() == 2


def _sliver_pipe(radius=0.028, length=1.0, n=38):
    """An open pipe tessellated the way OCP does a straight bore: n strips round it, each one
    quad the whole length cut along its diagonal into two slivers (4.6 mm by 1 m here)."""
    t = 2.0 * np.pi * np.arange(n) / n
    ring = np.stack([radius * np.cos(t), radius * np.sin(t), np.zeros(n)], axis=1)
    pts = np.concatenate([ring, ring + [0.0, 0.0, length]])
    i = np.arange(n)
    j = (i + 1) % n
    tri = np.concatenate([np.stack([i, j, n + j], 1), np.stack([i, n + j, n + i], 1)])
    return pts, tri


def _oriented(tri):
    """every edge used at most once in each direction: one winding over the whole sheet"""
    d = np.concatenate([tri[:, [0, 1]], tri[:, [1, 2]], tri[:, [2, 0]]])
    return len(np.unique(d, axis=0)) == len(d)


def _rim_points(pts, tri):
    fi, li = rim_edges(tri, len(pts))
    return np.unique(np.concatenate([tri[fi, li], tri[fi, (li + 1) % 3]]))


def test_a_sliver_tessellated_pipe_is_laddered_not_exploded():
    # TIME-17: abc 00168427 (a 4 m pipe, 56 mm bore) came from OCP as 284 slivers and left the
    # halving as 2,868,474 triangles. Halved, each 1 m sliver here becomes 3**7 = 2187 needles
    # (about 118 x area / h^2 for the pipe); laddered, 2 x 2**7 - 1 = 255 - its strip is the
    # CAD's own 4.6 mm wide, so that is 14 x area / h^2, and under the triangles vmtk's own
    # remesh makes of it at its target edge (0.15 x the radius).
    radius, length, h_wall, h_rim = 0.028, 1.0, 0.0112, 0.00466
    pts, tri = _sliver_pipe(radius, length)
    area = _areas(pts, tri).sum()
    p2, t2 = bound_edges(pts, tri, h_wall=h_wall, h_rim=h_rim)
    assert len(t2) <= 16.0 * area / h_wall ** 2
    assert len(t2) <= area / (0.433 * (0.15 * radius) ** 2)
    _sound(p2, t2, up=False)
    assert _oriented(t2)
    length_e, owners = _edge_stats(p2, t2)
    assert length_e.max() <= h_wall * (1 + 1e-9)
    assert _areas(p2, t2).sum() == pytest.approx(area)
    # the rims are the CAD's: the same 2 x 38 points, nothing added, nothing moved
    assert (p2[:len(pts)] == pts).all()
    assert set(_rim_points(p2, t2).tolist()) == set(range(len(pts)))
    assert _rim_length(p2, t2)[1] == 2 * 38


def test_a_needle_beside_a_halved_triangle_shares_its_points():
    # a fat triangle and a needle on one long edge: the fat one is halved, the needle laddered
    # on the very midpoints the halving put on that edge - no T-junction, the rim as it was
    pts = np.array([[0, 0, 0], [8, 0, 0], [4, -6, 0], [8, 0.3, 0]], dtype=float)
    tri = np.array([[0, 2, 1], [0, 1, 3]])           # fat below the x axis, needle above
    p2, t2 = bound_edges(pts, tri, h_wall=1.01, h_rim=100.0)
    _sound(p2, t2)
    assert _oriented(t2)
    perimeter = (np.linalg.norm(pts[2] - pts[0]) + np.linalg.norm(pts[1] - pts[2])
                 + np.linalg.norm(pts[3] - pts[1]) + np.linalg.norm(pts[0] - pts[3]))
    assert _rim_length(p2, t2)[0] == pytest.approx(perimeter)
    assert _areas(p2, t2).sum() == pytest.approx(_areas(pts, tri).sum())
    # move 1 alone: a ladder of 8 rungs (2 x 8 - 1); halved, it was 3**3 = 27 (move 2 then fans
    # the tip, which stands on the rim, as it always has)
    p1, t1 = _halve_long_edges(pts, tri, 1.01)
    assert (p1[t1][:, :, 1] > 1e-12).any(axis=1).sum() == 2 * 8 - 1
    assert len(t1) == 64 + 15


def test_a_sliver_wider_than_the_bound_is_split_once_then_laddered():
    # all three edges long: the four-way split first, then its children are needles
    pts = np.array([[0, 0, 0], [64, 0, 0], [0, 2, 0]], dtype=float)
    tri = np.array([[0, 1, 2]])
    p2, t2 = bound_edges(pts, tri, h_wall=1.5, h_rim=100.0)
    _sound(p2, t2)
    assert _oriented(t2)
    assert _edge_stats(p2, t2)[0].max() <= 1.5 * (1 + 1e-9)
    assert _areas(p2, t2).sum() == pytest.approx(64.0)
    assert len(t2) <= 4 * 63 + 16                   # four ladders of 63; halved: 4 x 3**5 = 972


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
def test_bad_lengths_are_refused(bad):
    pts, tri = _square()
    with pytest.raises(ValueError):
        bound_edges(pts, tri, h_wall=bad, h_rim=1.0)
    with pytest.raises(ValueError):
        bound_edges(pts, tri, h_wall=1.0, h_rim=bad)
    with pytest.raises(ValueError):
        bound_edges(pts, tri, h_wall=1.0, h_rim=1.0, aspect_max=0.5)
