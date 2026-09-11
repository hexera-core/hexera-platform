# Responsibility: Prove rim refinement splits only rim edges, keeps the surface manifold and never
#                 emits a degenerate triangle.
from __future__ import annotations

import numpy as np
import pytest

from meshpipeline.engines.vmtk.rims import refine_rims, rim_edges, split_long_edges


def _square():
    # unit square, two triangles: four rim edges of length 1, one interior diagonal
    pts = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], dtype=float)
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


def test_rim_edges_finds_only_single_owner_edges():
    pts, tri = _square()
    fi, li = rim_edges(tri, len(pts))
    assert len(fi) == 4                       # the diagonal (0,2) is shared and excluded


def test_long_rim_edges_are_split_to_the_target_length_and_nothing_else_changes():
    pts, tri = _square()
    p2, t2 = refine_rims(pts, tri, 0.25)
    length, n = _rim_length(p2, t2)
    assert n == 16 and abs(length - 4.0) < 1e-12          # 4 edges x 4 pieces, perimeter kept
    assert abs(_areas(p2, t2).sum() - 1.0) < 1e-12        # the surface itself is unchanged
    assert (_areas(p2, t2) > 1e-9).all()                  # no degenerate fan triangle
    # every edge is owned by 1 (rim) or 2 (interior) triangles: still a manifold sheet
    e = np.concatenate([t2[:, [0, 1]], t2[:, [1, 2]], t2[:, [2, 0]]])
    key = np.sort(e, axis=1)
    _, cnt = np.unique(key[:, 0] * len(p2) + key[:, 1], return_counts=True)
    assert set(cnt.tolist()) <= {1, 2}


def test_short_rims_are_left_untouched():
    pts, tri = _square()
    p2, t2 = refine_rims(pts, tri, 1.0)
    assert p2.shape == pts.shape and t2.shape == tri.shape


def test_only_the_touched_triangles_are_rebuilt():
    pts = np.array([[0, 0, 0], [3, 0, 0], [3, 0.1, 0], [0, 0.1, 0]], dtype=float)
    tri = np.array([[0, 1, 2], [0, 2, 3]])
    p2, t2 = refine_rims(pts, tri, 10.0)      # nothing longer than 10 -> identical
    assert len(t2) == 2
    p3, t3 = refine_rims(pts, tri, 1.0)       # the two long rim edges (length 3) split in three
    assert (_areas(p3, t3) > 1e-12).all()
    assert abs(_areas(p3, t3).sum() - 0.3) < 1e-12


def test_interior_edges_are_split_conformingly_when_asked():
    pts, tri = _square()
    p2, t2 = split_long_edges(pts, tri, 0.5)              # the diagonal (1.41) splits too
    assert abs(_areas(p2, t2).sum() - 1.0) < 1e-12
    assert (_areas(p2, t2) > 1e-9).all()
    e = np.concatenate([t2[:, [0, 1]], t2[:, [1, 2]], t2[:, [2, 0]]])
    key = np.sort(e, axis=1)
    _, cnt = np.unique(key[:, 0] * len(p2) + key[:, 1], return_counts=True)
    assert set(cnt.tolist()) <= {1, 2}                    # still a manifold sheet: no T-junction
    # 4 rim midpoints + 2 points on the diagonal + 2 fan centroids (spokes from a centroid may
    # exceed the bound; the remesher that follows evens those out)
    assert len(p2) == 4 + 4 + 2 + 2
    # rims-only leaves the diagonal alone
    p3, t3 = split_long_edges(pts, tri, 0.5, rims_only=True)
    assert len(p3) == 4 + 4 + 2


def test_a_long_sliver_becomes_short_pieces_not_a_powers_of_four_explosion():
    # a 10 x 0.1 strip: two slivers; bound the edges to 0.5
    pts = np.array([[0, 0, 0], [10, 0, 0], [10, 0.1, 0], [0, 0.1, 0]], dtype=float)
    tri = np.array([[0, 1, 2], [0, 2, 3]])
    p2, t2 = split_long_edges(pts, tri, 0.5)
    assert 40 <= len(t2) <= 200                            # ~3 long edges x 20 pieces, fanned
    assert abs(_areas(p2, t2).sum() - 1.0) < 1e-12
    assert (_areas(p2, t2) > 1e-12).all()


@pytest.mark.parametrize("h", [0.0, -1.0, float("nan")])
def test_bad_edge_length_is_refused(h):
    pts, tri = _square()
    with pytest.raises(ValueError):
        refine_rims(pts, tri, h)
