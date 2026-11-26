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
#: the rule a needle's ladder in move 1 follows: the staged wall's triangles follow its area, never
#: the CAD's tessellation (docs/RULES.md)
NEEDLE_LADDER_RULE = "TIME-17"


def _pieces(length: np.ndarray, h) -> np.ndarray:
    """ceil(L / h), with an edge already cut down to h counting as one piece."""
    return np.maximum(np.ceil(length / h - 1e-9), 1.0)


def bound_edges(points: np.ndarray, faces: np.ndarray, *, h_wall: float, h_rim: float,
                aspect_max: float = 6.0,
                max_triangles: int | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Cut a triangle sheet so that no edge is longer than `h_wall`, no open-rim edge longer than
    `h_rim`, and no thin triangle touching a rim - longest edge squared over twice its area
    above `aspect_max` - is fanned from farther than `aspect_max` pieces away. The sheet stays
    conforming (the same new points on both sides of every edge) and oriented.

    Two moves, in this order:
      1. every edge longer than h_wall is halved, round after round, and each triangle that lost
         edges is rebuilt by how many it lost (one - bisected; two - the tip triangle plus the
         quad cut along its shorter diagonal; three - the four-triangle midpoint subdivision), so
         a big triangle becomes small ones of the same shape - but a needle (two long edges, the
         third within h_wall) is cut once into the same points and laddered, never halved into
         3^k needles of its own shape (_halve_long_edges, NEEDLE_LADDER_RULE);
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
    pts, tri = _halve_long_edges(pts, tri, float(h_wall), max_triangles=max_triangles)
    return _fan_rims_and_needles(pts, tri, h_rim=float(h_rim),
                                 h_floor=min(float(h_rim), float(h_wall)),
                                 h_wall=float(h_wall), aspect_max=float(aspect_max))


class TooManyTriangles(ValueError):
    """Move 1 would make more triangles than the caller's planned work allows (`max_triangles`):
    raised before the round that passes it is made."""

    def __init__(self, planned: int, limit: int) -> None:
        super().__init__(f"the edge bound would make at least {planned:,} triangles, past {limit:,}")
        self.planned = int(planned)
        self.limit = int(limit)


def pieces_after_halving(length, h: float) -> np.ndarray:
    """2^k: the pieces round-after-round halving cuts an edge of `length` into, k the rounds it
    takes to come within h (the bound _halve_long_edges reads, 1 for an edge already within it)."""
    lv = np.asarray(length, dtype=np.float64)
    k = np.ceil(np.log2(np.maximum(lv / (h * (1.0 + 1e-9)), 1.0)) - 1e-12)
    return np.power(2, np.maximum(k, 0).astype(np.int64))


def _halve_long_edges(pts: np.ndarray, tri: np.ndarray, h: float, *,
                      max_triangles: int | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Move 1: midpoint halving until no edge is longer than h; conforming and shape-keeping -
    except for NEEDLES, which are laddered (NEEDLE_LADDER_RULE).

    A needle - exactly two edges longer than h, the third within h, and thin (longest edge
    squared over twice its area above NEEDLE_ASPECT) - is taken out of the halving the round it
    appears. Its two long edges are still cut into exactly the points the halving puts on them
    (2^k equal pieces, k the rounds an edge of that length takes, the same midpoints whoever cuts
    them: a neighbour still being halved, another needle, or the needle itself), so the sheet
    stays conforming and every rim keeps the points it always got; then the two chains are
    zipped into a ladder of rungs from its apex, each quad cut along its shorter diagonal. A
    needle of 2^k pieces a side becomes 2^(k+1) - 1 triangles; halved, it became 3^k of its own
    shape. WHY, 2026-11-22: OCP tessellates a 4 m pipe of 56 mm bore (abc 00168427) as 284
    triangles, slivers 4 m long and 4.6 mm wide; nine rounds of halving made 2,868,474 triangles
    of it (an 11 mm wall needs about 14,000), their short edges down to microns, and the radius
    field and vmtk's own remesh then ran until the run's 250-minute wall (00160152, 00184527 the
    same). Fat triangles keep the halving: its count is within four times area / h^2.
    `max_triangles`: the planned work the caller allows - before a round passes it (the round's
    own count, 1 to 4 per face, and every ladder already planned), TooManyTriangles is raised
    and nothing more is made."""
    needles: list[np.ndarray] = []           # rotated so the whole edge is ca, the apex b
    halved: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []    # (lo, hi, midpoint id)
    laddered = 0                             # the triangles the needles taken out will become
    rounds = 0
    while rounds < 64 and len(tri):    # every round halves the longest edge; 2**64 is past any real mesh
        nf = len(tri)
        e, inv, _cnt, _face_of, _local_of = _edge_table(tri, len(pts))
        length = np.linalg.norm(pts[e[:, 1]] - pts[e[:, 0]], axis=1)
        pick = length > h * (1.0 + 1e-9)      # an edge halved down to exactly h is done
        if not pick.any():
            break
        pf = pick.reshape(3, nf).T
        two = pf.sum(axis=1) == 2
        if two.any():
            lf = length.reshape(3, nf).T
            a, b, c = pts[tri[:, 0]], pts[tri[:, 1]], pts[tri[:, 2]]
            area2 = np.linalg.norm(np.cross(b - a, c - a), axis=1)
            longest = lf.max(axis=1)
            needle = two & (longest * longest > NEEDLE_ASPECT * area2)
            if needle.any():
                whole = np.argmin(pf[needle], axis=1)        # the one edge not to halve
                rot = np.array([[1, 2, 0], [2, 0, 1], [0, 1, 2]])[whole]
                needles.append(np.take_along_axis(tri[needle], rot, axis=1))
                # a ladder of n1 + n3 - 1, its long edges in 2^k pieces each
                long_pieces = np.where(pf[needle], pieces_after_halving(lf[needle], h), 0)
                laddered += int(long_pieces.sum()) - int(needle.sum())
                tri = tri[~needle]
                continue                                     # the same round, without them
        if max_triangles is not None:
            # the triangles this round makes, before it makes them: 1, 2, 3 or 4 per face by the
            # number of its edges halved, and the ladders already planned
            after = nf + int(pf.sum()) + laddered
            if after > max_triangles:
                raise TooManyTriangles(after, max_triangles)
        rounds += 1
        uniq = np.unique(inv[pick])
        mid_of = np.full(int(inv.max()) + 1, -1, dtype=np.int64)
        mid_of[uniq] = len(pts) + np.arange(len(uniq))
        first_row = np.full(int(inv.max()) + 1, -1, dtype=np.int64)
        first_row[inv[::-1]] = np.arange(len(inv))[::-1]
        rows = first_row[uniq]
        halved.append((np.minimum(e[rows, 0], e[rows, 1]), np.maximum(e[rows, 0], e[rows, 1]),
                       mid_of[uniq]))
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
    if max_triangles is not None and len(tri) + laddered > max_triangles:
        raise TooManyTriangles(len(tri) + laddered, max_triangles)     # the ladders, before they are made
    if not needles:
        return pts, tri
    return _ladder_needles(pts, tri, np.concatenate(needles), halved, h)


def _ladder_needles(pts: np.ndarray, tri: np.ndarray, needles: np.ndarray,
                    halved: list[tuple[np.ndarray, np.ndarray, np.ndarray]],
                    h: float) -> tuple[np.ndarray, np.ndarray]:
    """The needles of move 1 (rows a, b, c with the whole edge ca), each long edge cut at the
    midpoints the halving made on it - or would have made, made here once and shared - and the
    two chains from the apex b zipped into a ladder (see _halve_long_edges)."""
    mid: dict[tuple[int, int], int] = {}
    for lo, hi, m in halved:
        mid.update(zip(zip(lo.tolist(), hi.tolist()), m.tolist()))
    coords = [tuple(p) for p in pts.tolist()]
    bar = h * (1.0 + 1e-9)

    def chain(u: int, v: int) -> list[int]:
        key = (u, v) if u < v else (v, u)
        m = mid.get(key)
        if m is None:
            pu, pv = coords[u], coords[v]
            # the halving's own length test, term for term, so both sides of an edge agree
            if float(np.sqrt(sum((x - y) * (x - y) for x, y in zip(pu, pv)))) <= bar:
                return [u, v]
            # the halving's own midpoint, 0.5 * (p + q), whichever end it is read from
            m = len(coords)
            coords.append(tuple(0.5 * (x + y) for x, y in zip(pu, pv)))
            mid[key] = m
        return chain(u, m)[:-1] + chain(m, v)

    def dist(i: int, j: int) -> float:
        return float(sum((x - y) ** 2 for x, y in zip(coords[i], coords[j])))

    rows: list[list[int]] = []
    for a, b, c in needles.tolist():
        p = chain(b, a)
        q = chain(b, c)
        n1, n3 = len(p) - 1, len(q) - 1
        rows.append([p[1], b, q[1]])                         # the tip rung
        i = j = 1
        while i < n1 or j < n3:
            # one more triangle along the side whose new diagonal is shorter
            if j == n3 or (i < n1 and dist(p[i + 1], q[j]) <= dist(p[i], q[j + 1])):
                rows.append([p[i + 1], p[i], q[j]])
                i += 1
            else:
                rows.append([p[i], q[j], q[j + 1]])
                j += 1
    out = np.asarray(coords, dtype=np.float64)
    return out, np.concatenate([tri, np.asarray(rows, dtype=np.int64).reshape(-1, 3)])


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
