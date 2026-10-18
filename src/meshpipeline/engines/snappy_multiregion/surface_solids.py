# Responsibility: Read the solids of a multi-region assembly that arrived as a SURFACE (STL), one closed body per solid.
# Owns: the split of an unnamed triangle soup into the bodies it bounds - also where two solids share a face and
#       the file carries that face twice - without trusting the file's winding.
# Boundaries: pure array geometry (numpy/scipy) on triangles; it names no region and meshes nothing.
"""A multi-region STL is one of two things.

* Named solids (`solid fluid` ... `solid wall_solid` in an ASCII STL): each named solid is one region
  body, as the user named it (read by the caller).
* One unnamed soup (a binary STL): every solid's closed skin, written together. Where two solids
  touch (the fluid against its pipe wall), the shared face is written once per solid - the same
  triangles twice - and the edges along the seam carry four triangles.

The soup is split by its SIDES, not its triangles. Every triangle has two sides, each facing one of
the spaces the surface cuts the world into. Around every edge the triangles are ordered by angle;
two neighbours in that order bound the same space between them, so the side of each that faces that
wedge belongs to the same space. Walking those joins gives one closed, consistently oriented skin per
space: each solid's (its sides wound into it), the outside world's, and a zero-thickness sliver
between the two copies of a shared face. Nothing depends on how the file wound its triangles - a
staging step that rewrote some of them (a VTK round trip does) changes nothing.

Each finite space bounded from outside is a body; a skin bounding a space from inside (a cavity: the
hole a body leaves in the solid around it) joins the smallest body that strictly contains it.
"""
from __future__ import annotations

import numpy as np


def _weld(tris: np.ndarray, tol: float) -> tuple[np.ndarray, np.ndarray]:
    """(points, faces): the triangle corners merged where they lie within `tol` of each other
    (a KD-tree, so two copies of a corner never straddle a rounding boundary)."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    from scipy.spatial import cKDTree
    corners = tris.reshape(-1, 3)
    pairs = cKDTree(corners).query_pairs(tol, output_type="ndarray")
    n = len(corners)
    g = coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(n, n)) \
        if len(pairs) else coo_matrix((n, n))
    _, label = connected_components(g, directed=False)
    _, first, inv = np.unique(label, return_index=True, return_inverse=True)
    return corners[first], inv.reshape(-1, 3)


def _tolerance(t: np.ndarray, rel_tol: float) -> float:
    """Coincidence distance: relative to the part AND to how far it sits from the origin - an STL
    stores float32, whose rounding grows with the coordinate, not with the part."""
    flat = t.reshape(-1, 3)
    span = float(np.ptp(flat, axis=0).max()) if len(flat) else 1.0
    far = float(np.abs(flat).max()) if len(flat) else 1.0
    return rel_tol * max(span, far, 1e-12)


def _side_skins(pts: np.ndarray, f: np.ndarray) -> np.ndarray:
    """A label per triangle SIDE (2 * n: side 2i is the side triangle i's winding normal points
    out of, 2i + 1 the other): sides that face the same space share a label."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    nf = len(f)
    normal = np.cross(pts[f[:, 1]] - pts[f[:, 0]], pts[f[:, 2]] - pts[f[:, 0]])
    normal /= np.maximum(np.linalg.norm(normal, axis=1), 1e-300)[:, None]
    a = np.concatenate([f[:, 0], f[:, 1], f[:, 2]])
    b = np.concatenate([f[:, 1], f[:, 2], f[:, 0]])
    c = np.concatenate([f[:, 2], f[:, 0], f[:, 1]])       # the third corner of the face
    face = np.tile(np.arange(nf), 3)
    lo, hi = np.minimum(a, b), np.maximum(a, b)
    order = np.lexsort((hi, lo))
    lo, hi, face, c = lo[order], hi[order], face[order], c[order]
    starts = np.flatnonzero(np.r_[True, (lo[1:] != lo[:-1]) | (hi[1:] != hi[:-1])])
    ends = np.r_[starts[1:], len(lo)]
    rows: list[int] = []
    cols: list[int] = []
    for s, e in zip(starts, ends, strict=True):
        k = e - s
        if k < 2:
            continue                                      # an open rim: nothing to join across
        p0 = pts[lo[s]]
        axis = pts[hi[s]] - p0
        axis /= max(float(np.linalg.norm(axis)), 1e-300)
        u = np.cross(axis, [1.0, 0.0, 0.0])
        if np.linalg.norm(u) < 1e-6:
            u = np.cross(axis, [0.0, 1.0, 0.0])
        u /= np.linalg.norm(u)
        v = np.cross(axis, u)
        fins = []
        for j in range(s, e):
            d = pts[c[j]] - p0
            d = d - np.dot(d, axis) * axis               # the fin, across the edge
            theta = float(np.arctan2(np.dot(d, v), np.dot(d, u)))
            tangent = np.cross(axis, d)                  # counter-clockwise about the edge here
            # the side whose normal turns counter-clockwise faces the wedge ahead of this fin
            ahead = 0 if float(np.dot(normal[face[j]], tangent)) > 0.0 else 1
            fins.append((theta, int(face[j]), ahead))
        fins.sort(key=lambda x: x[0])
        for pos in range(k):
            th_i, fi, ahead_i = fins[pos]
            th_j, fj, ahead_j = fins[(pos + 1) % k]
            # the wedge from fin i to fin j: i's side facing ahead, j's side facing back
            rows.append(2 * fi + ahead_i)
            cols.append(2 * fj + (1 - ahead_j))
    n = 2 * nf
    g = coo_matrix((np.ones(len(rows)), (rows, cols)), shape=(n, n))
    _, label = connected_components(g, directed=False)
    return label


def _oriented(t: np.ndarray, side: np.ndarray) -> np.ndarray:
    """The triangles wound so their normal points AWAY from the space the given sides face."""
    out = t.copy()
    flip = side == 0          # side 0 faces along the winding normal: flip so it faces back
    out[flip] = out[flip][:, ::-1]
    return out


def split_shells(tris, *, rel_tol: float = 1e-6) -> list[np.ndarray]:
    """The closed bodies a triangle soup bounds, as outward-wound (n, 3, 3) arrays, largest first;
    a cavity skin is part of the body around it."""
    t = np.asarray(tris, dtype=float).reshape(-1, 3, 3)
    if len(t) == 0:
        return []
    pts, f = _weld(t, _tolerance(t, rel_tol))
    # A face two solids share is written once per solid. Its two copies are ONE surface with a
    # solid on each side - and a VTK round trip (binary STL staging) even rewinds the second copy
    # to match the first, so nothing tells them apart. Kept once, its two sides face the two solids.
    _, keep = np.unique(np.sort(f, axis=1), axis=0, return_index=True)
    f = f[np.sort(keep)]
    f = f[(f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 0] != f[:, 2])]
    tw = pts[f]                                          # welded triangles, file winding
    label = _side_skins(pts, f)
    skins = []
    span = float(np.ptp(tw.reshape(-1, 3), axis=0).max()) or 1.0
    for lab in np.unique(label):
        sides = np.flatnonzero(label == lab)
        tri_idx, side = sides // 2, sides % 2
        skin = _oriented(tw[tri_idx], side)              # normals point out of the space
        fx = shell_facts(skin)
        if fx["volume"] <= 1e-9 * span ** 3:
            continue                                     # the sliver between two copies of a face
        skins.append((skin, fx))
    bodies = [(s, fx) for s, fx in skins if fx["signed_volume"] > 0]   # a space bounded outside
    holes = [(s, fx) for s, fx in skins if fx["signed_volume"] <= 0]   # a cavity, or the world
    groups = [[s] for s, _ in bodies]
    for s, fx in holes:
        lo, hi = np.asarray(fx["bbox_min"]), np.asarray(fx["bbox_max"])
        tol = 1e-6 * span
        hosts = [i for i, (_, b) in enumerate(bodies)
                 if (np.asarray(b["bbox_min"]) < lo - tol).all()
                 and (np.asarray(b["bbox_max"]) > hi + tol).all()]
        if hosts:                                        # the world's own skin has no host
            groups[min(hosts, key=lambda i: bodies[i][1]["volume"])].append(s)
    shells = [np.concatenate(g) for g in groups]
    return sorted(shells, key=len, reverse=True)


def shell_facts(shell: np.ndarray) -> dict:
    """volume (by the divergence theorem, positive for an outward-wound closed shell), centroid
    of the enclosed volume, bounding box."""
    s = np.asarray(shell, dtype=float)
    v0, v1, v2 = s[:, 0], s[:, 1], s[:, 2]
    cross = np.cross(v1, v2)
    vol6 = np.einsum("ij,ij->i", v0, cross)
    vol = float(vol6.sum()) / 6.0
    cen = ((vol6[:, None] * (v0 + v1 + v2)).sum(axis=0) / (4.0 * vol6.sum())
           if abs(vol6.sum()) > 0 else s.reshape(-1, 3).mean(axis=0))
    flat = s.reshape(-1, 3)
    return {"volume": abs(vol), "signed_volume": vol, "centroid": [float(x) for x in cen],
            "bbox_min": [float(x) for x in flat.min(axis=0)],
            "bbox_max": [float(x) for x in flat.max(axis=0)]}


def is_closed(shell: np.ndarray, rel_tol: float = 1e-6) -> bool:
    """Every edge of the shell is shared by exactly two of its triangles."""
    t = np.asarray(shell, dtype=float).reshape(-1, 3, 3)
    _, f = _weld(t, _tolerance(t, rel_tol))
    e = np.sort(np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]]), axis=1)
    _, cnt = np.unique(e, axis=0, return_counts=True)
    return bool(len(cnt)) and bool((cnt == 2).all())


__all__ = ["is_closed", "shell_facts", "split_shells"]
