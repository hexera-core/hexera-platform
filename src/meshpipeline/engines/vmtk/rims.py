# Responsibility: Bound the edge length and the shape of a staged lumen so vmtk's remesher gets
#                 sane triangles - no port rim it can collapse, no needle converging on a rim.
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


#: longest edge squared over twice the area: a thin triangle above this is a needle, and needles
#: are zipped rather than fanned (see bound_edges). 6 is thin; a 50 x 0.8 mm stub needle is 62.
NEEDLE_ASPECT = 20.0


def _pieces(length: np.ndarray, h) -> np.ndarray:
    """ceil(L / h), with an edge already cut down to h counting as one piece."""
    return np.maximum(np.ceil(length / h - 1e-9), 1.0)


def bound_edges(points: np.ndarray, faces: np.ndarray, *, h_wall: float, h_rim: float,
                aspect_max: float = 6.0) -> tuple[np.ndarray, np.ndarray]:
    """Cut a triangle sheet so that no edge is longer than `h_wall`, no open-rim edge longer than
    `h_rim`, and no thin triangle touching a rim - longest edge squared over twice its area
    above `aspect_max` - is fanned from farther than `aspect_max` pieces away. The sheet stays
    conforming (the same new points on both sides of every edge) and oriented.

    Two moves, in this order:
      1. every edge longer than h_wall is halved, round after round, and each triangle that lost
         edges is rebuilt by how many it lost (one - bisected; two - the tip triangle plus the
         quad cut along its shorter diagonal; three - the four-triangle midpoint subdivision), so
         a big triangle becomes small ones of the same shape;
      2. rim edges longer than h_rim, and the long edges of thin triangles touching a rim, are
         cut into equal pieces and each touched triangle is fanned from its centroid - the fan
         puts squat triangles at the rim points, and after move 1 no spoke exceeds two thirds of
         h_wall. A thin triangle asks for pieces of two thirds its length over aspect_max,
         floored at the finer of h_rim and h_wall so nothing is cut finer than the remesh wants.
         A true NEEDLE (aspect above NEEDLE_ASPECT, its short edge whole) is not fanned but
         zipped: its two chains become a ladder of short rungs and the tip rung is fanned from
         its centre, so the rim point at its tip only sees tiny triangles.
    Move 2 runs once: its wedges are what the remesher has always been fed, and fanning a fan
    again only makes flatter wedges (a lone needle went from 22 triangles to 356 in 8 rounds).

    WHY, 2026-09-11: OCP tessellates a straight cylinder as slivers spanning its whole length, a
    flat 545 mm wall as a handful of giant triangles and the foot of a manifold stub as
    50 x 0.8 mm needles whose tips sit on the port rim. vmtksurfaceremeshing turns thin or
    skewed triangles at a rim into duplicate points and non-manifold zero-area triangles, and
    vmtkmeshgenerator segfaults in its own remesh on that surface - whether the rim is left
    open, fixed (-preserveboundary 1) or capped first. Cutting only the original edges and
    fanning from the centroid left every 5 mm rim piece of transition_013 fanned from a vertex
    340 mm away (move 1 fixes that); move 1 alone left manifold_002's needles whole (move 2
    fixes that); zipping EVERY rim-touching thin triangle into a ladder put skewed rungs,
    obtuse at the rim points, on the flat outlet wall of s_duct_001 and it pinched there, while
    fanning the needles standing on manifold_002's outlet rim (30 mm wedges with a one-degree
    apex at its seam point, from the centroid or from just inside the tip) pinched that point -
    the ladder with its tiny tip fan was the one treatment that left it clean. Hence the split
    by aspect: wide-but-thin triangles are fanned, needles are zipped. Halving everything down
    to the remesh length made 1.1 million triangles of transition_013; halving and then fanning
    every interior edge at the remesh length pinched transition_013 again."""
    pts = np.asarray(points, dtype=np.float64)
    tri = np.asarray(faces, dtype=np.int64).reshape(-1, 3)
    for name, h in (("h_wall", h_wall), ("h_rim", h_rim)):
        if not (h > 0.0) or not np.isfinite(h):
            raise ValueError(f"edge refinement needs a positive finite {name}, got {h!r}")
    if not (aspect_max >= 1.0):
        raise ValueError(f"aspect_max must be at least 1, got {aspect_max!r}")
    if len(tri) == 0:
        return pts, tri
    pts, tri = _halve_long_edges(pts, tri, float(h_wall))
    return _fan_rims_and_needles(pts, tri, h_rim=float(h_rim),
                                 h_floor=min(float(h_rim), float(h_wall)),
                                 h_wall=float(h_wall), aspect_max=float(aspect_max))


def _halve_long_edges(pts: np.ndarray, tri: np.ndarray, h: float) -> tuple[np.ndarray, np.ndarray]:
    """Move 1: midpoint halving until no edge is longer than h; conforming and shape-keeping."""
    for _ in range(64):     # every round halves the longest edge; 2**64 is past any real mesh
        nf = len(tri)
        e, inv, _cnt, _face_of, _local_of = _edge_table(tri, len(pts))
        length = np.linalg.norm(pts[e[:, 1]] - pts[e[:, 0]], axis=1)
        pick = length > h * (1.0 + 1e-9)      # an edge halved down to exactly h is done
        if not pick.any():
            break
        uniq = np.unique(inv[pick])
        mid_of = np.full(int(inv.max()) + 1, -1, dtype=np.int64)
        mid_of[uniq] = len(pts) + np.arange(len(uniq))
        first_row = np.full(int(inv.max()) + 1, -1, dtype=np.int64)
        first_row[inv[::-1]] = np.arange(len(inv))[::-1]
        rows = first_row[uniq]
        pts = np.concatenate([pts, 0.5 * (pts[e[rows, 0]] + pts[e[rows, 1]])])
        m = mid_of[inv].reshape(3, nf).T          # per face: midpoint id of edges ab, bc, ca
        code = (m[:, 0] >= 0) * 1 + (m[:, 1] >= 0) * 2 + (m[:, 2] >= 0) * 4
        out = [tri[code == 0]]
        s = code == 7
        a, b, c = tri[s, 0], tri[s, 1], tri[s, 2]
        m0, m1, m2 = m[s, 0], m[s, 1], m[s, 2]
        out += [np.stack([a, m0, m2], 1), np.stack([m0, b, m1], 1),
                np.stack([m2, m1, c], 1), np.stack([m0, m1, m2], 1)]
        # one halved edge: rotate so it is ab, then bisect from c
        for value, rot in ((1, (0, 1, 2)), (2, (1, 2, 0)), (4, (2, 0, 1))):
            s = code == value
            a, b, c = tri[s, rot[0]], tri[s, rot[1]], tri[s, rot[2]]
            m0 = m[s, rot[0]]
            out += [np.stack([a, m0, c], 1), np.stack([m0, b, c], 1)]
        # two halved edges: rotate so the whole one is ca; tip triangle at b, quad a-m0-m1-c
        for value, rot in ((3, (0, 1, 2)), (6, (1, 2, 0)), (5, (2, 0, 1))):
            s = code == value
            a, b, c = tri[s, rot[0]], tri[s, rot[1]], tri[s, rot[2]]
            m0, m1 = m[s, rot[0]], m[s, rot[1]]
            out.append(np.stack([m0, b, m1], 1))
            short_am1 = (np.linalg.norm(pts[a] - pts[m1], axis=1)
                         <= np.linalg.norm(pts[m0] - pts[c], axis=1))
            out += [np.stack([a, m0, m1], 1)[short_am1], np.stack([a, m1, c], 1)[short_am1],
                    np.stack([a, m0, c], 1)[~short_am1], np.stack([m0, m1, c], 1)[~short_am1]]
        tri = np.concatenate([o for o in out if len(o)]).astype(np.int64)
    return pts, tri


def _fan_rims_and_needles(pts: np.ndarray, tri: np.ndarray, *, h_rim: float, h_floor: float,
                          h_wall: float, aspect_max: float) -> tuple[np.ndarray, np.ndarray]:
    """Move 2: cut rim edges to h_rim and the edges of rim-touching thin triangles to their own
    piece length, then fan every touched triangle from its centroid."""
    nf = len(tri)
    e, inv, cnt, _face_of, _local_of = _edge_table(tri, len(pts))
    length = np.linalg.norm(pts[e[:, 1]] - pts[e[:, 0]], axis=1)
    on_rim = cnt[inv] == 1
    wish = np.where(on_rim, _pieces(length, h_rim), 1.0)
    lf = length.reshape(3, nf).T
    a, b, c = pts[tri[:, 0]], pts[tri[:, 1]], pts[tri[:, 2]]
    area = 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1)
    longest = lf.max(axis=1)
    rim_pts = np.zeros(len(pts), dtype=bool)
    rim_pts[e[on_rim]] = True
    touching = rim_pts[tri].any(axis=1)
    thin = (longest * longest > 2.0 * aspect_max * area) & touching
    needle = (longest * longest > 2.0 * NEEDLE_ASPECT * area) & touching
    h_t = np.clip(2.0 * longest / (3.0 * aspect_max), h_floor, h_wall)
    wish = np.maximum(wish, _pieces(length, np.tile(np.where(thin, h_t, np.inf), 3)))
    n_unique = int(inv.max()) + 1
    pieces = np.ones(n_unique, dtype=np.int64)
    np.maximum.at(pieces, inv, wish.astype(np.int64))
    if (pieces <= 1).all():
        return pts, tri
    # the split points of every cut edge, ordered from its smaller point id to its larger
    first_row = np.full(n_unique, -1, dtype=np.int64)
    first_row[inv[::-1]] = np.arange(len(inv))[::-1]
    u_ids = np.flatnonzero(pieces > 1)
    rows = first_row[u_ids]
    lo = np.minimum(e[rows, 0], e[rows, 1])
    hi = np.maximum(e[rows, 0], e[rows, 1])
    k = pieces[u_ids]
    start = np.full(n_unique, -1, dtype=np.int64)
    start[u_ids] = len(pts) + np.concatenate([[0], np.cumsum(k - 1)[:-1]])
    rep = np.repeat(np.arange(len(u_ids)), k - 1)
    t = (np.concatenate([np.arange(1, kk) for kk in k]) / np.repeat(k, k - 1))[:, None]
    pts = np.concatenate([pts, pts[lo[rep]] + (pts[hi[rep]] - pts[lo[rep]]) * t])
    uid_f = inv.reshape(3, nf).T           # per face, per local edge: the unique edge id
    split = (tri, uid_f, pieces, start)
    cut_f = pieces[uid_f] > 1
    touched = np.flatnonzero(cut_f.any(axis=1))
    out: list[np.ndarray] = [tri[~cut_f.any(axis=1)]]
    rest: list[list[int]] = []
    centres: list[np.ndarray] = []
    short_of = lf.argmin(axis=1)
    cid0 = len(pts)
    for fi in touched:
        cid = cid0 + len(centres)
        if needle[fi] and cut_f[fi].sum() == 2 and not cut_f[fi, short_of[fi]]:
            r = (int(short_of[fi]) - 1) % 3                      # rotate: the whole edge is bc
            p = _chain(split, fi, r)                             # a -> b
            q = _chain(split, fi, (r + 2) % 3)[::-1]             # a -> c
            k1, k3 = len(p) - 1, len(q) - 1
            centres.append((pts[p[0]] + pts[p[1]] + pts[q[1]]) / 3.0)
            rest += [[p[0], p[1], cid], [p[1], q[1], cid], [q[1], p[0], cid]]   # the tip rung
            i = j = 1
            while i < k1 or j < k3:
                if j == k3 or (i < k1 and (i + 1) * k3 <= (j + 1) * k1):
                    rest.append([p[i], p[i + 1], q[j]])
                    i += 1
                else:
                    rest.append([p[i], q[j + 1], q[j]])
                    j += 1
            continue
        poly = _chain(split, fi, 0)[:-1] + _chain(split, fi, 1)[:-1] + _chain(split, fi, 2)[:-1]
        centres.append(pts[tri[fi]].mean(axis=0))
        rest += [[cid, poly[i], poly[(i + 1) % len(poly)]] for i in range(len(poly))]
    pts = np.concatenate([pts, np.asarray(centres)])
    out.append(np.asarray(rest, dtype=np.int64))
    tri = np.concatenate([o for o in out if len(o)]).astype(np.int64)
    return pts, tri


def _chain(split: tuple, fi: int, le: int) -> list[int]:
    """Point ids from vertex le to vertex le+1 of face fi, the edge's split points included."""
    tri, uid_f, pieces, start = split
    u, v = int(tri[fi, le]), int(tri[fi, (le + 1) % 3])
    uid = int(uid_f[fi, le])
    kk = int(pieces[uid])
    if kk == 1:
        return [u, v]
    s = int(start[uid])
    inner = list(range(s, s + kk - 1))
    if u > v:
        inner.reverse()
    return [u, *inner, v]
