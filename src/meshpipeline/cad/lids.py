# Responsibility: Close one loop of a triangle surface with a lid: the triangles that span an open
# end (or a bore's mouth) so the fluid region behind it is closed. Any loop shape, flat or not.
# Owns: how a loop is triangulated - in the plane it is drawn on when it is flat, as the projected
# triangulation pulled toward the smallest area when it is not - and the proof that the lid's own
# triangles do not cross each other.
# Boundaries: numpy over one loop's points. It knows nothing about ports, names or engines; the
# caller decides which loops get lids and what each lid is called.
from __future__ import annotations

import math

import numpy as np

#: A loop that strays from its plane by less than this share of its size is flat: its lid is the
#: plane's own triangulation, with no pull toward a smaller area.
FLAT = 1e-3
#: How many projection directions are tried before a loop is closed with a fan from its centre.
_TILTS_DEG = (0.0, 10.0, 20.0, 35.0, 50.0)


class LidError(RuntimeError):
    """A loop that cannot be closed by any triangulation this module knows."""


def frame(P: np.ndarray) -> tuple[np.ndarray, np.ndarray, float, float]:
    """A closed 3D loop measured about itself: area centroid, unit normal (Newell, so its sense
    follows the loop's winding), the area it spans, and how far it strays from that plane."""
    P = np.asarray(P, dtype=float)
    q = np.roll(P, -1, axis=0)
    n = np.array([np.sum((P[:, 1] - q[:, 1]) * (P[:, 2] + q[:, 2])),
                  np.sum((P[:, 2] - q[:, 2]) * (P[:, 0] + q[:, 0])),
                  np.sum((P[:, 0] - q[:, 0]) * (P[:, 1] + q[:, 1]))])
    L = float(np.linalg.norm(n))
    if L <= 0:
        raise LidError("the loop spans no area")
    n = n / L
    u, v = _basis(n)
    mean = P.mean(axis=0)
    x, y = (P - mean) @ u, (P - mean) @ v
    x1, y1 = np.roll(x, -1), np.roll(y, -1)
    cr = x * y1 - x1 * y
    a2 = float(cr.sum())
    if abs(a2) <= 0:
        c = mean
    else:
        c = mean + (float(((x + x1) * cr).sum()) / (3 * a2)) * u + (float(((y + y1) * cr).sum()) / (3 * a2)) * v
    dev = float(np.max(np.abs((P - c) @ n)))
    return c, n, L / 2.0, dev


def _basis(n: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    u = np.cross(n, [0.0, 0.0, 1.0] if abs(n[2]) < 0.9 else [0.0, 1.0, 0.0])
    u /= np.linalg.norm(u)
    return u, np.cross(n, u)


def lid(P: np.ndarray) -> tuple[np.ndarray, np.ndarray, dict]:
    """Triangles closing the loop P (m x 3, in order, no point repeated).

    Returns (extra points, triangles, facts): the triangles index P's points (0..m-1) and then the
    extra points (m..), and wind the same way as P - so a lid on a hole whose neighbouring faces
    run the loop's edges the other way already agrees with them. A lid uses the loop's own points
    on its edge, so the surface stays closed along it to the last bit.

    A flat loop is triangulated in its plane, edges flipped until no triangle is needlessly thin
    (the plane's Delaunay triangulation of the loop). A loop that is not flat is triangulated in a
    direction it projects without crossing itself, then its edges are flipped toward the smallest
    area - a minimal-surface lid - only where the flip keeps every triangle on its own patch of the
    projection, which is what keeps the lid from passing through itself."""
    P = np.asarray(P, dtype=float)
    m = len(P)
    if m < 3:
        raise LidError("a loop needs three points")
    if m == 3:
        return np.zeros((0, 3)), np.array([[0, 1, 2]]), {"method": "triangle", "planarity": 0.0}
    c, n, area, dev = frame(P)
    size = 2.0 * math.sqrt(max(area, 1e-300) / math.pi)
    planarity = dev / size if size > 0 else 0.0
    for tilt in _TILTS_DEG:
        for d in _directions(n, tilt):
            u, v = _basis(d)
            xy = np.c_[(P - c) @ u, (P - c) @ v]
            if _signed_area(xy) <= 0:
                continue                          # seen from behind in this direction
            if not _simple(xy):
                continue
            tris = _earclip(xy, P)
            if tris is None:
                continue
            tris = _delaunay_flips(xy, tris)
            if not _tiles(xy, tris):
                continue
            extra, xy_all, fine = _refine(xy, P, tris)
            if planarity > FLAT:
                fine = _area_flips(xy_all, np.vstack([P, extra]), fine)
            if not _tiles(xy_all, fine, area=_signed_area(xy)):
                # the refinement or the pull toward the smallest area folded a triangle over
                # another (a near-straight run of the rim): the lid of the loop's own points stands
                extra, fine = np.zeros((0, 3)), tris
            return extra, fine, {"method": "projected" if tilt else "planar",
                                 "planarity": round(planarity, 5), "tilt_deg": tilt,
                                 "inner_points": int(len(extra))}
    # NO DIRECTION SEES THE LOOP WITHOUT IT CROSSING ITSELF: a fan from a point at the loop's centre,
    # pulled off its plane by the loop's own mean height, closes it. The caller checks the result.
    centre = P.mean(axis=0)
    k = np.arange(m)
    tris = np.c_[k, (k + 1) % m, np.full(m, m)]
    return centre[None, :], tris, {"method": "fan", "planarity": round(planarity, 5)}


#: A lid's triangles are refined until none is wider (by circumradius) than this many times the
#: loop's own mean edge, with at most REFINE_POINTS_PER_EDGE x (loop points) points added inside:
#: a lid made of the loop's points alone is a fan of slivers across the mouth, which a mesher that
#: remeshes the boundary (gmsh) parametrises badly and fills with poor tets.
REFINE_RATIO = 1.3
REFINE_POINTS_PER_EDGE = 4
REFINE_MAX_POINTS = 6000


def _refine(xy: np.ndarray, P: np.ndarray, tris: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Points added inside the lid where its triangles are too wide - each at the centroid of a
    triangle, at that triangle's own height, so the lid keeps its shape - with Delaunay flips after
    each round. Returns (the added 3D points, every point in the projection, the triangles over P's
    points then the added ones)."""
    m = len(P)
    h = float(np.mean(np.linalg.norm(np.roll(P, -1, axis=0) - P, axis=1)))
    if h <= 0:
        return np.zeros((0, 3)), xy, tris
    budget = min(REFINE_POINTS_PER_EDGE * m, REFINE_MAX_POINTS)
    xy_all, p_all = xy.copy(), P.copy()
    for _round in range(12):
        a, b, c = (xy_all[tris[:, k]] for k in range(3))
        la, lb, lc = (np.linalg.norm(x - y, axis=1) for x, y in ((b, c), (c, a), (a, b)))
        area2 = np.abs(_cross(a, b, c))
        circ = la * lb * lc / np.maximum(2.0 * area2, 1e-300)
        bad = np.flatnonzero(circ > REFINE_RATIO * h)
        # never a point hard by the rim: there a sliver along a near-straight run of the loop
        # would be split into triangles too thin to flip safely
        if len(bad):
            bad = bad[_dist_to_loop((a[bad] + b[bad] + c[bad]) / 3.0, xy) > 0.5 * h]
        room = budget - (len(p_all) - m)
        if len(bad) == 0 or room <= 0:
            break
        bad = bad[np.argsort(-circ[bad])][:room]
        ids = len(p_all) + np.arange(len(bad))
        t = tris[bad]
        keep = np.ones(len(tris), dtype=bool)
        keep[bad] = False
        split = np.concatenate([np.c_[t[:, 0], t[:, 1], ids], np.c_[t[:, 1], t[:, 2], ids],
                                np.c_[t[:, 2], t[:, 0], ids]])
        xy_all = np.vstack([xy_all, (a[bad] + b[bad] + c[bad]) / 3.0])
        p_all = np.vstack([p_all, p_all[t].mean(axis=1)])
        tris = _delaunay_flips(xy_all, np.vstack([tris[keep], split]))
    return p_all[m:], xy_all, tris


def _dist_to_loop(pts: np.ndarray, loop: np.ndarray) -> np.ndarray:
    """Distance from each 2D point to the closed polyline `loop`."""
    a, b = loop, np.roll(loop, -1, axis=0)
    ab = b - a
    L2 = np.maximum(np.einsum("ij,ij->i", ab, ab), 1e-300)
    out = np.full(len(pts), np.inf)
    for s in range(0, len(pts), 256):
        p = pts[s:s + 256, None, :]
        t = np.clip(np.einsum("pij,ij->pi", p - a[None], ab) / L2, 0.0, 1.0)
        q = a[None] + t[..., None] * ab[None]
        out[s:s + 256] = np.linalg.norm(p - q, axis=2).min(axis=1)
    return out


def ring(P: np.ndarray, Q: np.ndarray) -> np.ndarray:
    """Triangles between an outer loop P and an inner loop Q inside it - a ring-shaped opening,
    the gap around a centre body. Both are seen in P's own plane about P's centre and stitched in
    order of angle, each triangle taking the next point of whichever loop is behind; the result
    indexes P's points (0..m-1) then Q's (m..) and winds the same way as P. Raises LidError when
    either loop does not turn once around the centre (then no stitch can be a ring)."""
    P, Q = np.asarray(P, dtype=float), np.asarray(Q, dtype=float)
    c, n, _area, _dev = frame(P)
    u, v = _basis(n)

    def angles(X):
        a = np.unwrap(np.arctan2((X - c) @ v, (X - c) @ u))
        return a

    def ccw(X):
        a = angles(X)
        turn = (a[-1] - a[0]) + _wrap(a[0] - a[-1])
        return turn > 0

    qi = np.arange(len(Q)) if ccw(Q) else np.arange(len(Q))[::-1]
    for X in (P, Q[qi]):
        a = angles(X)
        if np.any(np.diff(a) <= 0) or not (abs((a[-1] - a[0]) + _wrap(a[0] - a[-1]) - 2 * math.pi) < 1e-6):
            raise LidError("a loop of the ring does not turn once around its centre")
    m, k = len(P), len(Q)
    aP = angles(P)
    aQ = angles(Q[qi])
    # start both at the point nearest angle aP[0]
    s = int(np.argmin(np.abs(_wrap(aQ - aP[0]))))
    qord = np.roll(qi, -s)
    aQ = np.roll(aQ, -s)
    aQ = aQ - 2 * math.pi * np.round((aQ[0] - aP[0]) / (2 * math.pi))
    aQ = np.unwrap(aQ)
    aP2 = np.r_[aP, aP[0] + 2 * math.pi]
    aQ2 = np.r_[aQ, aQ[0] + 2 * math.pi]
    i = j = 0
    tris = []
    while i < m or j < k:
        take_p = j >= k or (i < m and aP2[i + 1] <= aQ2[j + 1])
        pi, qj = i % m, m + int(qord[j % k])
        if take_p:
            tris.append((pi, (i + 1) % m, qj))
            i += 1
        else:
            tris.append((pi, m + int(qord[(j + 1) % k]), qj))
            j += 1
    out = np.asarray(tris, dtype=np.int64)
    # wind with P: P runs counter-clockwise about n, so every triangle must too
    pts = np.vstack([P, Q])
    nn = np.cross(pts[out[:, 1]] - pts[out[:, 0]], pts[out[:, 2]] - pts[out[:, 0]]) @ n
    out[nn < 0] = out[nn < 0][:, [0, 2, 1]]
    return out


def _wrap(a):
    return (np.asarray(a) + math.pi) % (2 * math.pi) - math.pi


def _tiles(xy: np.ndarray, tris: np.ndarray, area: float | None = None) -> bool:
    """Whether the triangles tile the polygon in the projection: every one turns the same way
    with room to spare, and together they cover its area exactly - so none lies over another."""
    a, b, c = (xy[tris[:, k]] for k in range(3))
    signed = 0.5 * _cross(a, b, c)
    whole = _signed_area(xy) if area is None else area
    if np.any(signed <= 0.0):
        return False
    return abs(float(signed.sum()) - whole) <= 1e-9 * abs(whole)


def _directions(n: np.ndarray, tilt_deg: float) -> list[np.ndarray]:
    if tilt_deg == 0.0:
        return [n]
    u, v = _basis(n)
    t = math.radians(tilt_deg)
    return [math.cos(t) * n + math.sin(t) * (math.cos(a) * u + math.sin(a) * v)
            for a in np.linspace(0.0, 2.0 * math.pi, 8, endpoint=False)]


def _signed_area(xy: np.ndarray) -> float:
    x, y = xy[:, 0], xy[:, 1]
    return 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def _simple(xy: np.ndarray) -> bool:
    """Whether the closed polygon crosses or touches itself anywhere but at neighbouring edges."""
    m = len(xy)
    a, b = xy, np.roll(xy, -1, axis=0)
    span = float(np.max(np.ptp(xy, axis=0))) or 1.0
    eps = 1e-12 * span * span
    idx = np.arange(m)
    lo_x, hi_x = np.minimum(a[:, 0], b[:, 0]), np.maximum(a[:, 0], b[:, 0])
    lo_y, hi_y = np.minimum(a[:, 1], b[:, 1]), np.maximum(a[:, 1], b[:, 1])
    order = np.argsort(lo_x)
    for start in range(0, m, 512):
        rows = order[start:start + 512]
        # only segments whose boxes overlap can meet
        ov = ((lo_x[rows, None] <= hi_x[None, :]) & (lo_x[None, :] <= hi_x[rows, None])
              & (lo_y[rows, None] <= hi_y[None, :]) & (lo_y[None, :] <= hi_y[rows, None]))
        i_, j_ = np.nonzero(ov)
        i = rows[i_]
        j = j_
        keep = (i < j) & (j != (i + 1) % m) & (i != (j + 1) % m)
        i, j = i[keep], j[keep]
        if len(i) == 0:
            continue
        p, q, r, s = a[i], b[i], a[j], b[j]
        d1 = _cross(p, q, r)
        d2 = _cross(p, q, s)
        d3 = _cross(r, s, p)
        d4 = _cross(r, s, q)
        proper = (((d1 > eps) & (d2 < -eps)) | ((d1 < -eps) & (d2 > eps))) & \
                 (((d3 > eps) & (d4 < -eps)) | ((d3 < -eps) & (d4 > eps)))
        if proper.any():
            return False
        # a point of one segment lying on the other (a pinch): not a simple loop either
        touch = ((np.abs(d1) <= eps) & _on(p, q, r)) | ((np.abs(d2) <= eps) & _on(p, q, s)) | \
                ((np.abs(d3) <= eps) & _on(r, s, p)) | ((np.abs(d4) <= eps) & _on(r, s, q))
        if touch.any():
            return False
    del idx
    return True


def _cross(o: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return (a[..., 0] - o[..., 0]) * (b[..., 1] - o[..., 1]) - (a[..., 1] - o[..., 1]) * (b[..., 0] - o[..., 0])


def _on(p: np.ndarray, q: np.ndarray, r: np.ndarray) -> np.ndarray:
    return ((np.minimum(p[:, 0], q[:, 0]) <= r[:, 0]) & (r[:, 0] <= np.maximum(p[:, 0], q[:, 0]))
            & (np.minimum(p[:, 1], q[:, 1]) <= r[:, 1]) & (r[:, 1] <= np.maximum(p[:, 1], q[:, 1])))


def _earclip(xy: np.ndarray, P: np.ndarray) -> np.ndarray | None:
    """Ear clipping of a simple counter-clockwise polygon; the ear with the shortest 3D diagonal
    goes first, so the lid hugs its loop. None when the polygon runs out of ears (it is not simple
    after all, to rounding)."""
    m = len(xy)
    prev = np.roll(np.arange(m), 1)
    nxt = np.roll(np.arange(m), -1)
    alive = np.ones(m, dtype=bool)
    span = float(np.max(np.ptp(xy, axis=0))) or 1.0
    eps = 1e-14 * span * span

    def ear(i: int) -> bool:
        a, b, c = prev[i], i, nxt[i]
        if _cross(xy[a], xy[b], xy[c]) <= eps:
            return False                                  # reflex or straight: not an ear
        live = np.flatnonzero(alive)
        live = live[(live != a) & (live != b) & (live != c)]
        if len(live) == 0:
            return True
        q = xy[live]
        inside = ((_cross(xy[a], xy[b], q) >= -eps) & (_cross(xy[b], xy[c], q) >= -eps)
                  & (_cross(xy[c], xy[a], q) >= -eps))
        # a point AT a corner of the ear (a loop that visits a spot twice) does not block it
        same = (np.all(np.isclose(q, xy[a]), axis=1) | np.all(np.isclose(q, xy[b]), axis=1)
                | np.all(np.isclose(q, xy[c]), axis=1))
        return not bool(np.any(inside & ~same))

    is_ear = np.array([ear(i) for i in range(m)])
    diag = np.linalg.norm(P[prev] - P[nxt], axis=1)
    out = []
    left = m
    while left > 3:
        cand = np.flatnonzero(is_ear & alive)
        if len(cand) == 0:
            # an ear the last clip uncovered without touching its neighbours: look again
            live = np.flatnonzero(alive)
            is_ear[live] = [ear(i) for i in live]
            cand = np.flatnonzero(is_ear & alive)
            if len(cand) == 0:
                return None
        i = int(cand[np.argmin(diag[cand])])
        a, c = int(prev[i]), int(nxt[i])
        out.append((a, i, c))
        alive[i] = False
        nxt[a], prev[c] = c, a
        left -= 1
        for k in (a, c):
            diag[k] = float(np.linalg.norm(P[prev[k]] - P[nxt[k]]))
            is_ear[k] = ear(k)
    live = np.flatnonzero(alive)
    i = int(live[0])
    out.append((int(prev[i]), i, int(nxt[i])))
    return np.asarray(out, dtype=np.int64)


def _edge_map(tris: np.ndarray) -> dict:
    em: dict = {}
    for t, (a, b, c) in enumerate(tris.tolist()):
        for p, q in ((a, b), (b, c), (c, a)):
            em.setdefault((min(p, q), max(p, q)), []).append(t)
    return em


def _flip_pass(xy: np.ndarray, tris: np.ndarray, better) -> np.ndarray:
    """Flip interior edges while `better` says so. Each flip keeps both triangles counter-clockwise
    in the projection, so the triangulation stays a triangulation of the same polygon."""
    tris = tris.copy()
    em = _edge_map(tris)
    todo = [e for e, ts in em.items() if len(ts) == 2]
    budget = 20 * len(tris) + 100
    span = float(np.max(np.ptp(xy, axis=0))) or 1.0
    eps = 1e-12 * span * span
    while todo and budget > 0:
        budget -= 1
        e = todo.pop()
        ts = em.get(e)
        if not ts or len(ts) != 2:
            continue
        t1, t2 = ts
        p, q = e
        r = [v for v in tris[t1] if v not in e][0]
        s = [v for v in tris[t2] if v not in e][0]
        # the quad p-r-q-s must be convex in the projection, with room to spare against rounding,
        # for the flip to be legal
        c1, c2 = _cross(xy[r], xy[s], xy[p]), _cross(xy[r], xy[s], xy[q])
        c3, c4 = _cross(xy[p], xy[q], xy[r]), _cross(xy[p], xy[q], xy[s])
        if not (c1 * c2 < 0 and c3 * c4 < 0 and min(abs(c1), abs(c2), abs(c3), abs(c4)) > eps):
            continue
        if not better(p, q, r, s):
            continue
        n1 = _ccw(xy, (r, s, p))
        n2 = _ccw(xy, (s, r, q))
        old = [tuple(tris[t1]), tuple(tris[t2])]
        tris[t1], tris[t2] = n1, n2
        for tri in old:
            for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
                k = (min(a, b), max(a, b))
                if k in em:
                    em[k] = [t for t in em[k] if t not in (t1, t2)]
        for t, tri in ((t1, n1), (t2, n2)):
            for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
                em.setdefault((min(a, b), max(a, b)), []).append(t)
        for a, b in ((p, r), (r, q), (q, s), (s, p)):
            todo.append((min(a, b), max(a, b)))
    return tris


def _ccw(xy: np.ndarray, t: tuple) -> tuple:
    a, b, c = t
    return (a, b, c) if _cross(xy[a], xy[b], xy[c]) > 0 else (a, c, b)


def _delaunay_flips(xy: np.ndarray, tris: np.ndarray) -> np.ndarray:
    def better(p, q, r, s) -> bool:
        # flip when the two angles facing the edge sum past 180 degrees (the empty-circle test)
        return _angle(xy, r, p, q) + _angle(xy, s, p, q) > math.pi + 1e-9
    return _flip_pass(xy, tris, better)


def _angle(xy: np.ndarray, at: int, a: int, b: int) -> float:
    u, v = xy[a] - xy[at], xy[b] - xy[at]
    nu, nv = float(np.hypot(*u)), float(np.hypot(*v))
    if nu <= 0 or nv <= 0:
        return math.pi
    return math.acos(max(-1.0, min(1.0, float(u @ v) / (nu * nv))))


def _area_flips(xy: np.ndarray, P: np.ndarray, tris: np.ndarray) -> np.ndarray:
    def area(a, b, c) -> float:
        return 0.5 * float(np.linalg.norm(np.cross(P[b] - P[a], P[c] - P[a])))

    def better(p, q, r, s) -> bool:
        before = area(p, q, r) + area(p, q, s)
        return area(r, s, p) + area(r, s, q) < before * (1.0 - 1e-9)
    return _flip_pass(xy, tris, better)


__all__ = ["FLAT", "LidError", "frame", "lid"]
