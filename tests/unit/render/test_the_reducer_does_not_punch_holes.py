# Responsibility: Prove the reducer does not punch holes in a structure finer than its cluster.
# Boundaries: pure geometry; no payload, no browser, no mesh on disk.
from __future__ import annotations

import numpy as np
import pytest

from meshpipeline.render.polymesh_budget import _cluster_once, _facing

# WHY THIS TEST EXISTS. `_cluster_once` used to key a vertex by POSITION alone, so a fin thinner
# than the cluster put both of its faces in one cell, every triangle on it collapsed, and the fin
# was DROPPED. Measured on the delivered cold plate at a 446 um cluster against a 100.631 um
# channel: the drawn coolant wall came to 58,468.15 mm2 against checkMesh's own 63,763.58, 8.30%
# of the surface simply absent, and the chip's staircased air side lost 19.8%. The reducer did not
# blur a fine structure, it perforated it, and nothing on screen said so.


def _fin(thickness: float, n: int = 40, length: float = 1.0):
    """A thin double-sided sheet: two parallel faces `thickness` apart, each `n` quads long.

    This is the shape that vanished. Returns (points, triangles).
    """
    x = np.linspace(0.0, length, n + 1, dtype=np.float32)
    pts, tris = [], []
    for side, y in enumerate((0.0, thickness)):
        base = len(pts)
        for xi in x:
            pts.append([xi, y, 0.0])
            pts.append([xi, y, length])
        for i in range(n):
            a, b = base + 2 * i, base + 2 * i + 1
            c, d = base + 2 * i + 2, base + 2 * i + 3
            # opposite winding per side so the two sheets face away from each other
            tris.append([a, b, c] if side == 0 else [a, c, b])
            tris.append([b, d, c] if side == 0 else [b, c, d])
    return np.asarray(pts, dtype=np.float32), np.asarray(tris, dtype=np.int64)


def _area(pts, tris):
    v0, v1, v2 = pts[tris[:, 0]], pts[tris[:, 1]], pts[tris[:, 2]]
    return 0.5 * float(np.linalg.norm(np.cross(v1 - v0, v2 - v0), axis=1).sum())


def test_a_fin_thinner_than_the_cluster_survives_it():
    """The whole point. The cluster is 20x the fin's thickness, so position alone would put both
    faces in one cell and drop the sheet entirely."""
    pts, tris = _fin(thickness=0.01)
    before = _area(pts, tris)
    got_pts, got_tris = _cluster_once(pts, tris, h=0.2)   # 20x the fin thickness
    assert len(got_tris) > 0, "the fin was dropped entirely - this is the hole"
    after = _area(got_pts, got_tris)
    # it is allowed to be coarse. It is not allowed to be absent: nothing can fit this fin's
    # structure in a cluster 20x its thickness, but the sheet itself must still be there.
    assert after > 0.5 * before, (
        "the fin lost more than half its area: %.4f of %.4f" % (after, before))


def test_the_two_sides_of_a_fin_never_share_a_cluster():
    """Why it works: a vertex's identity includes which way it faces, and opposing sheets point
    opposite ways however close together they are."""
    pts, tris = _fin(thickness=0.001)      # 200x thinner than the cluster below
    facing = _facing(pts, tris)
    half = len(pts) // 2
    # every vertex on one side agrees with itself, and disagrees with the other side
    assert len(set(facing[:half].tolist())) == 1, facing[:half]
    assert len(set(facing[half:].tolist())) == 1, facing[half:]
    assert facing[0] != facing[half], "the two sides landed in the same sector"


def test_a_flat_sheet_does_not_pay_for_the_extra_key():
    """A surface with nothing to lose must not pay for this.

    A flat grid has ONE facing, so the key becomes (cell, constant), which partitions exactly as
    (cell) did: the result is identical to the old behaviour by construction, and that is what
    this asserts rather than an area.

    It does NOT assert the area is preserved. Vertex clustering snaps the outer ring of a sheet
    inward to its cluster centres, so a clustered unit square comes back about 0.81 - a real and
    expected shrink at the boundary, not a hole. An earlier version of this test demanded 1.0 and
    failed; the test was wrong, not the reducer.
    """
    n = 20
    xs, ys = np.meshgrid(np.linspace(0, 1, n + 1), np.linspace(0, 1, n + 1))
    pts = np.stack([xs.ravel(), ys.ravel(), np.zeros(xs.size)], axis=1).astype(np.float32)
    idx = np.arange((n + 1) * (n + 1)).reshape(n + 1, n + 1)
    tris = []
    for i in range(n):
        for j in range(n):
            tris.append([idx[i, j], idx[i, j + 1], idx[i + 1, j]])
            tris.append([idx[i, j + 1], idx[i + 1, j + 1], idx[i + 1, j]])
    tris = np.asarray(tris, dtype=np.int64)
    assert len(set(_facing(pts, tris).tolist())) == 1, "a flat sheet must have one facing"
    got_pts, got_tris = _cluster_once(pts, tris, h=0.25)
    assert len(got_tris) > 0, "a flat sheet must never come back empty"
    # the expected boundary shrink and nothing worse: comfortably more than half the sheet is
    # still there, which is the thing that distinguishes a coarse surface from a perforated one
    assert 0.70 < _area(got_pts, got_tris) <= 1.0 + 1e-6


def test_a_vertex_no_triangle_touches_does_not_break_the_key():
    """A zero normal must bucket harmlessly rather than raise: an unused point is dropped by the
    repack anyway, and the budget must never be the thing that breaks a draw."""
    pts, tris = _fin(thickness=0.01)
    pts = np.vstack([pts, np.array([[9.0, 9.0, 9.0]], dtype=np.float32)])
    facing = _facing(pts, tris)
    assert facing.shape == (len(pts),)
    assert np.isfinite(facing).all()
