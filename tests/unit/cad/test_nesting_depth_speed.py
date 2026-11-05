# Responsibility: Hold the faster nesting count (cad/fea_features.nesting_depth) to the SAME answer as the
# straightforward one it replaced, on nested closed pieces and on a loose triangle soup.
# Boundaries: numpy only; the reference below is the earlier code, kept here as the oracle.
from __future__ import annotations

import numpy as np

from meshpipeline.cad import fea_features as ff

# THE DEFECT THIS PINS. Shared dev corpus, 1 Nov: a 2.8 MB elbow STL that welds into 29,049 loose pieces spent
# over 30 minutes counting which pieces enclose which (every pair re-selected its triangles from the whole
# surface and tested all of them). The same count now takes about a minute.


def _reference(xyz, tris, comp):
    n = int(comp.max()) + 1 if len(comp) else 0
    depth = np.zeros(n, dtype=np.int64)
    if n < 2:
        return depth
    pts = xyz[tris]
    lo = np.full((n, 3), np.inf)
    hi = np.full((n, 3), -np.inf)
    np.minimum.at(lo, comp, pts.min(axis=1))
    np.maximum.at(hi, comp, pts.max(axis=1))
    first = np.zeros(n, dtype=np.int64)
    first[comp[::-1]] = np.arange(len(comp))[::-1]
    d = np.array([0.3119, 0.5741, 0.7570])
    d /= np.linalg.norm(d)
    for i in range(n):
        o = xyz[tris[first[i], 0]]
        for j in range(n):
            if j == i or np.any(lo[i] < lo[j]) or np.any(hi[i] > hi[j]):
                continue
            tj = pts[comp == j]
            a, e1, e2 = tj[:, 0], tj[:, 1] - tj[:, 0], tj[:, 2] - tj[:, 0]
            p = np.cross(d, e2)
            det = np.einsum("ij,ij->i", p, e1)
            ok = np.abs(det) > 1e-14
            inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
            tv = o - a
            u = np.einsum("ij,ij->i", tv, p) * inv
            q = np.cross(tv, e1)
            v = (q @ d) * inv
            t = np.einsum("ij,ij->i", e2, q) * inv
            if int(np.count_nonzero(ok & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > 0))) % 2 == 1:
                depth[i] += 1
    return depth


def _cube(c, h):
    from meshpipeline.cad.stl_io import _box_triangles

    return np.asarray(_box_triangles(tuple(np.subtract(c, h)), tuple(np.add(c, h))), dtype=float)


def _pieces(blocks):
    tris = np.concatenate(blocks)
    xyz = tris.reshape(-1, 3)
    t = np.arange(len(xyz)).reshape(-1, 3)
    comp = np.concatenate([np.full(len(b), k, dtype=np.int64) for k, b in enumerate(blocks)])
    return xyz, t, comp


def test_nested_cubes_count_the_same():
    xyz, t, comp = _pieces([_cube((0, 0, 0), 10), _cube((0, 0, 0), 6), _cube((1, 1, 1), 2), _cube((30, 0, 0), 3)])
    got = ff.nesting_depth(xyz, t, comp)
    assert got.tolist() == _reference(xyz, t, comp).tolist() == [0, 1, 2, 0]


def test_a_loose_triangle_soup_counts_the_same():
    rng = np.random.default_rng(7)
    blocks = [_cube((0, 0, 0), 50)]
    soup = rng.uniform(-60, 60, size=(300, 1, 3)) + rng.uniform(-3, 3, size=(300, 3, 3))
    blocks += [tri[None] for tri in soup.reshape(300, 3, 3)]
    blocks += [_cube((5, -5, 5), 20), _cube((5, -5, 5), 4)]
    xyz, t, comp = _pieces(blocks)
    assert ff.nesting_depth(xyz, t, comp).tolist() == _reference(xyz, t, comp).tolist()
