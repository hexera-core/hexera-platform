# Responsibility: Prove bound_edges cuts the rims fine and the interior coarse, fans wide thin
#                 triangles and zips needles that touch a rim, keeps the sheet conforming and
#                 oriented, and never emits a degenerate triangle.
from __future__ import annotations

import numpy as np
import pytest

from meshpipeline.engines.vmtk.rims import bound_edges, rim_edges


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


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
def test_bad_lengths_are_refused(bad):
    pts, tri = _square()
    with pytest.raises(ValueError):
        bound_edges(pts, tri, h_wall=bad, h_rim=1.0)
    with pytest.raises(ValueError):
        bound_edges(pts, tri, h_wall=1.0, h_rim=bad)
    with pytest.raises(ValueError):
        bound_edges(pts, tri, h_wall=1.0, h_rim=1.0, aspect_max=0.5)
