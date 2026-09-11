# Responsibility: Bound the edge length of a staged lumen so vmtk's remesher gets sane triangles -
#                 no port rim it can collapse, no cylinder sliver it can corrupt.
# Boundaries: pure array geometry (numpy only); it knows nothing about CAD, ports or vmtk itself.
from __future__ import annotations

import numpy as np


def _edge_table(faces: np.ndarray, n_points: int):
    """Per (face, local edge): the edge's endpoints, its unique-edge index and owner count."""
    nf = len(faces)
    e = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    key = np.sort(e, axis=1)
    key = key[:, 0] * int(n_points) + key[:, 1]
    _, inv, cnt = np.unique(key, return_inverse=True, return_counts=True)
    face_of = np.tile(np.arange(nf), 3)
    local_of = np.repeat(np.arange(3), nf)
    return e, inv, cnt, face_of, local_of


def rim_edges(faces: np.ndarray, n_points: int) -> tuple[np.ndarray, np.ndarray]:
    """(face index, local edge index) of every edge that belongs to exactly one triangle."""
    faces = np.asarray(faces, dtype=np.int64)
    if len(faces) == 0:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64)
    _, inv, cnt, face_of, local_of = _edge_table(faces, n_points)
    on_rim = cnt[inv] == 1
    return face_of[on_rim], local_of[on_rim]


def split_long_edges(points: np.ndarray, faces: np.ndarray, h: float, *,
                     rims_only: bool = False) -> tuple[np.ndarray, np.ndarray]:
    """Split every edge longer than `h` into ceil(L/h) equal pieces - the SAME new points on both
    sides of an interior edge, so the sheet stays conforming - and re-triangulate every triangle
    that lost an edge as a fan from its centroid (never degenerate: the centroid is strictly
    inside, the new points lie on the old edges). Untouched triangles are left exactly as they
    were. `rims_only` restricts the split to edges owned by a single triangle.

    WHY NOT A SUBDIVISION FILTER: OCP tessellates a straight cylinder as slivers spanning its
    whole length (a 2.6 m tee: min angle 0.001 degrees) and vmtksurfaceremeshing leaves zero-area
    non-manifold triangles on the branch-port rims of such input, on which TetGen dies. A 4-way
    adaptive subdivision that halves every edge of an offending triangle turned 434 triangles
    into 1.1 million (a 443 s remesh); splitting only the long edges gives ~300 per sliver.
    WHY THE RIMS: a CAD port rim is as coarse as the port face - a rectangle is four vertices -
    and vmtksurfaceremeshing either kept those as giant edges (preserveboundary=1: a two-triangle
    cap, a broken Voronoi diagram) or collapsed the loop outright (preserveboundary=0 sealed one
    opening of bend_elbow_003, 2026-09-11)."""
    pts = np.asarray(points, dtype=np.float64)
    tri = np.asarray(faces, dtype=np.int64).reshape(-1, 3)
    if not (h > 0.0) or not np.isfinite(h):
        raise ValueError(f"edge refinement needs a positive finite edge length, got {h!r}")
    if len(tri) == 0:
        return pts, tri
    e, inv, cnt, face_of, local_of = _edge_table(tri, len(pts))
    length = np.linalg.norm(pts[e[:, 1]] - pts[e[:, 0]], axis=1)
    pick = length > h
    if rims_only:
        pick &= cnt[inv] == 1
    if not pick.any():
        return pts, tri
    # one point row per UNIQUE edge, keyed by the sorted pair, ordered from the smaller id
    new_pts: list[np.ndarray] = [pts]
    next_id = len(pts)
    split: dict[tuple[int, int], list[int]] = {}
    for row in np.flatnonzero(pick):
        a, b = int(e[row, 0]), int(e[row, 1])
        lo, hi = (a, b) if a < b else (b, a)
        if (lo, hi) in split:
            continue
        k = int(np.ceil(float(length[row]) / h))
        if k <= 1:
            continue
        t = (np.arange(1, k, dtype=np.float64) / k)[:, None]
        new_pts.append(pts[lo] + (pts[hi] - pts[lo]) * t)
        split[(lo, hi)] = list(range(next_id, next_id + k - 1))
        next_id += k - 1
    if not split:
        return pts, tri
    touched = np.zeros(len(tri), dtype=bool)
    touched[face_of[pick]] = True
    out: list[list[int]] = [f.tolist() for f in tri[~touched]]
    centroids: list[np.ndarray] = []
    for fi in np.flatnonzero(touched):
        f = tri[fi]
        poly: list[int] = []
        for le in range(3):
            u, v = int(f[le]), int(f[(le + 1) % 3])
            poly.append(u)
            ids = split.get((u, v) if u < v else (v, u))
            if ids:
                poly.extend(ids if u < v else ids[::-1])   # walk the edge from u towards v
        cid = next_id + len(centroids)
        centroids.append(pts[f].mean(axis=0))
        for i in range(len(poly)):
            out.append([cid, poly[i], poly[(i + 1) % len(poly)]])
    new_pts.append(np.asarray(centroids))
    return np.concatenate(new_pts), np.asarray(out, dtype=np.int64)


def refine_rims(points: np.ndarray, faces: np.ndarray, h: float) -> tuple[np.ndarray, np.ndarray]:
    """split_long_edges restricted to the open rims."""
    return split_long_edges(points, faces, h, rims_only=True)
