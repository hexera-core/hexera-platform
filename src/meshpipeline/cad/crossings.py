# Responsibility: Find where a triangle surface passes through itself - every pair of triangles
# that cross - fast enough to run on every upload.
# Owns: the broad phase (a grid of boxes, one level per triangle size) and the narrow phase (an
# edge of one triangle piercing the inside of the other).
# Boundaries: numpy over welded triangles. It says which pairs cross, not what that means: the
# clean check (cad/validity) turns pairs into reasons, and the B-rep check confirms them on the
# exact faces. Deterministic: the same triangles give the same pairs, and every limit is a count
# of work, never a clock.
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

#: The most candidate pairs (boxes sharing a grid cell) one call may look at. A typical surface
#: yields 15-30 per triangle, most of them set aside by two dot products (each must reach the
#: other's plane) before any real test; a big flat triangle over a dense patch yields thousands.
#: Past this the search stops and says it is incomplete - it never guesses.
MAX_CANDIDATE_PAIRS = 500_000_000
#: Pairs handled at once: bounds the memory of one batch (about 1 KB of arrays per pair).
BATCH_PAIRS = 2_000_000
#: The most crossing pairs kept. Enough to place every damaged spot; past it the count is "more".
MAX_HITS = 20_000
#: Distances below this share of a pair's size count as zero: faces that touch along an edge or
#: at a corner, or lie in one plane (a body resting on another), are not crossings.
EPS = 1e-9
#: Two planes this close to parallel (sine of the angle) have no common line to cut.
PARALLEL = 1e-12

_BITS = 21
_MAXC = (1 << _BITS) - 1


@dataclass(frozen=True)
class Crossings:
    """The crossing pairs found (i < j, triangle indices), and whether the search saw every pair."""

    pairs: np.ndarray
    complete: bool
    tested: int

    @property
    def count(self) -> int:
        return int(len(self.pairs))


def find_crossings(points: np.ndarray, triangles: np.ndarray, *,
                   max_pairs: int = MAX_CANDIDATE_PAIRS, tolerance: float = 0.0) -> Crossings:
    """Every pair of triangles of a welded surface that pass through each other.

    Triangles that share a corner are neighbours and never tested (a fold through a shared corner
    is left to the orientation and manifold checks). `points` (n, 3), `triangles` (m, 3) indices.
    `tolerance`: a distance below which a corner counts as ON the other triangle's plane - the
    file's own number precision (a 32-bit file cannot place two touching faces in one plane any
    closer), so faces that touch are never read as crossing."""
    P = np.asarray(points, dtype=np.float64)
    T = np.asarray(triangles, dtype=np.int64)
    m = len(T)
    empty = np.zeros((0, 2), dtype=np.int64)
    if m < 2:
        return Crossings(empty, True, 0)
    C = P[T]
    lo, hi = C.min(axis=1), C.max(axis=1)
    ext = (hi - lo).max(axis=1)
    gmin = lo.min(axis=0)
    diag = float(np.linalg.norm(hi.max(axis=0) - gmin))
    if not np.isfinite(diag) or diag <= 0.0:
        return Crossings(empty, True, 0)
    pos = ext[ext > 0]
    h0 = float(np.median(pos)) if pos.size else diag
    # each piece's plane and its box as a ball: a pair whose box-ball misses the other's plane
    # cannot cross, and two dot products say so
    nrm = np.cross(C[:, 1] - C[:, 0], C[:, 2] - C[:, 0])
    nl = np.linalg.norm(nrm, axis=1)
    unit = np.where(nl[:, None] > 0, nrm / np.where(nl > 0, nl, 1.0)[:, None], 0.0)
    geo = (unit, -np.einsum("ij,ij->i", unit, C[:, 0]), (lo + hi) / 2.0, np.linalg.norm(hi - lo, axis=1) / 2.0)
    # a triangle lives on the level whose cells are just big enough for its box: it then covers at
    # most two cells along each axis, so a long sliver never floods a fine grid and a dense patch
    # of small triangles never crowds one coarse cell. The finest level still numbers under 2^21
    # cells along each axis.
    finest = int(math.ceil(math.log2(diag / ((_MAXC - 4) * h0))))
    with np.errstate(divide="ignore"):
        level = np.ceil(np.log2(np.maximum(ext, 1e-300) / h0))
    level = np.maximum(level, finest).astype(np.int64)
    tol = max(float(tolerance), 0.0)
    hits: list[np.ndarray] = []
    n_hits = 0
    tested = 0
    complete = True
    for L in np.unique(level).tolist():
        h = h0 * (2.0 ** L)
        own = np.flatnonzero(level == L)
        okeys, otri = _entries(own, lo, hi, gmin, h)
        batches = [_same_cell_pairs(okeys, otri)]
        low = np.flatnonzero(level < L)
        if low.size:
            batches.append(_cross_pairs(okeys, otri, low, lo, hi, gmin, h))
        for gen in batches:
            for I, J, K in gen:
                tested += len(I)
                if tested > max_pairs:
                    complete = False
                    break
                found = _narrow(T, C, lo, hi, geo, gmin, h, I, J, K, tol)
                if len(found):
                    hits.append(found)
                    n_hits += len(found)
                if n_hits >= MAX_HITS:
                    break
            if not complete or n_hits >= MAX_HITS:
                break
        if not complete or n_hits >= MAX_HITS:
            break
    pairs = np.unique(np.vstack(hits), axis=0) if hits else empty
    return Crossings(pairs[:MAX_HITS], complete, tested)


def _pack(c: np.ndarray) -> np.ndarray:
    c = np.clip(c, 0, _MAXC).astype(np.int64)
    return (c[..., 0] << (2 * _BITS)) | (c[..., 1] << _BITS) | c[..., 2]


def _entries(idx: np.ndarray, lo, hi, gmin, h: float) -> tuple[np.ndarray, np.ndarray]:
    """(cell key, triangle) for every cell the triangles' boxes touch at cell size h (at most 8)."""
    if idx.size == 0:
        return np.zeros(0, np.int64), np.zeros(0, np.int64)
    a = np.floor((lo[idx] - gmin) / h).astype(np.int64)
    b = np.floor((hi[idx] - gmin) / h).astype(np.int64)
    b = np.minimum(b, a + 1)                     # a box no bigger than a cell spans at most two
    keys, tris = [], []
    for dx in (0, 1):
        for dy in (0, 1):
            for dz in (0, 1):
                d = np.array([dx, dy, dz])
                c = a + d
                ok = np.all(c <= b, axis=1)
                keys.append(_pack(c[ok]))
                tris.append(idx[ok])
    return np.concatenate(keys), np.concatenate(tris)


def _runs(sorted_keys: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    starts = np.flatnonzero(np.r_[True, sorted_keys[1:] != sorted_keys[:-1]])
    return starts, np.diff(np.r_[starts, len(sorted_keys)])


def _same_cell_pairs(keys: np.ndarray, tris: np.ndarray):
    """Batches of (i, j, cell key) for every two triangles sharing a cell."""
    if len(keys) < 2:
        return
    order = np.argsort(keys, kind="stable")
    k, v = keys[order], tris[order]
    starts, sizes = _runs(k)
    keep = sizes > 1
    starts, sizes = starts[keep], sizes[keep]
    if not len(starts):
        return
    # each member pairs with the members after it in its cell
    pos = _expand(starts, sizes)
    end = np.repeat(starts + sizes, sizes)
    cnt = end - pos - 1
    yield from _emit(pos, cnt, lambda a, off: (v[a], v[a + 1 + off], k[a]))


def _cross_pairs(okeys: np.ndarray, otri: np.ndarray, low: np.ndarray, lo, hi, gmin, h: float):
    """Batches of (i, j, cell key): a triangle of this level against every smaller one sharing a
    cell. The smaller ones are keyed only where a cell of this level is occupied."""
    if not len(okeys) or not len(low):
        return
    occupied = np.unique(okeys)
    # a small box's cells are its corner cell and the next one along each axis, so it can meet an
    # occupied cell only if its corner cell is that cell or one step before it
    base = np.floor((lo[low] - gmin) / h).astype(np.int64)
    near = np.zeros(len(low), dtype=bool)
    for dx in (0, 1):
        for dy in (0, 1):
            for dz in (0, 1):
                near |= np.isin(_pack(base + np.array([dx, dy, dz])), occupied)
    lkeys, ltri = _entries(low[near], lo, hi, gmin, h)
    keep = np.isin(lkeys, occupied)
    lkeys, ltri = lkeys[keep], ltri[keep]
    if not len(lkeys):
        return
    lorder = np.argsort(lkeys, kind="stable")
    lk, lv = lkeys[lorder], ltri[lorder]
    oorder = np.argsort(okeys, kind="stable")
    ok, ov = okeys[oorder], otri[oorder]
    first = np.searchsorted(lk, ok, side="left")
    last = np.searchsorted(lk, ok, side="right")
    cnt = last - first
    pos = np.arange(len(ok))
    yield from _emit(pos, cnt, lambda a, off: (ov[a], lv[first[a] + off], ok[a]))


def _expand(starts: np.ndarray, sizes: np.ndarray) -> np.ndarray:
    total = int(sizes.sum())
    offs = np.arange(total) - np.repeat(np.cumsum(sizes) - sizes, sizes)
    return np.repeat(starts, sizes) + offs


def _emit(pos: np.ndarray, cnt: np.ndarray, make):
    """Expand 'element pos[r] pairs with cnt[r] partners' into batches of at most BATCH_PAIRS."""
    keep = cnt > 0
    pos, cnt = pos[keep], cnt[keep]
    if not len(pos):
        return
    csum = np.cumsum(cnt)
    start = 0
    while start < len(pos):
        base = csum[start - 1] if start else 0
        stop = int(np.searchsorted(csum, base + BATCH_PAIRS, side="right"))
        stop = max(stop, start + 1)
        c = cnt[start:stop]
        a = np.repeat(pos[start:stop], c)
        off = np.arange(int(c.sum())) - np.repeat(np.cumsum(c) - c, c)
        yield make(a, off)
        start = stop


def _narrow(T, C, lo, hi, geo, gmin, h, I, J, K, tol: float) -> np.ndarray:
    """The pairs of one batch that really cross, as (min, max) rows."""
    # each pair is judged once: in the cell holding the low corner of where the two boxes overlap
    ref = _pack(np.floor((np.maximum(lo[I], lo[J]) - gmin) / h).astype(np.int64))
    sel = (ref == K) & (I != J)
    sel &= np.all(lo[I] <= hi[J], axis=1) & np.all(lo[J] <= hi[I], axis=1)
    if not sel.any():
        return np.zeros((0, 2), dtype=np.int64)
    I, J = I[sel], J[sel]
    unit, off, mid, rad = geo
    reach = (np.abs(np.einsum("ij,ij->i", unit[I], mid[J]) + off[I]) <= rad[J] + tol) &         (np.abs(np.einsum("ij,ij->i", unit[J], mid[I]) + off[J]) <= rad[I] + tol)
    I, J = I[reach], J[reach]
    ti, tj = T[I], T[J]
    shared = np.zeros(len(I), dtype=bool)
    for a in range(3):
        for b in range(3):
            shared |= ti[:, a] == tj[:, b]
    I, J = I[~shared], J[~shared]
    if not len(I):
        return np.zeros((0, 2), dtype=np.int64)
    hit = _cross(C[I], C[J], tol)
    if not hit.any():
        return np.zeros((0, 2), dtype=np.int64)
    return np.stack([np.minimum(I[hit], J[hit]), np.maximum(I[hit], J[hit])], axis=1)


def _cross(A: np.ndarray, B: np.ndarray, tol: float = 0.0) -> np.ndarray:
    """Whether triangles A and B (k, 3, 3) pass THROUGH each other, per pair.

    Moller's interval test, made strict: each triangle must have corners on both sides of the
    other's plane (a face resting on a face, an edge lying on a face, or two faces in one plane
    are touching, not crossing), and the two stretches where they cut the common line must
    overlap by more than a hair. Unlike an edge-pierces-face test, this also catches two
    triangles whose edges cross each other exactly - common where a CAD exporter tessellates two
    bodies the same way."""
    nA = np.cross(A[:, 1] - A[:, 0], A[:, 2] - A[:, 0])
    nB = np.cross(B[:, 1] - B[:, 0], B[:, 2] - B[:, 0])
    span = np.maximum(np.max(np.ptp(np.concatenate([A, B], axis=1), axis=1), axis=1), 1e-300)
    dA = np.einsum("kij,kj->ki", A - B[:, :1], nB)          # A's corners against B's plane
    dB = np.einsum("kij,kj->ki", B - A[:, :1], nA)          # B's corners against A's plane
    near = np.maximum(EPS * span, tol)[:, None]                # a distance that counts as on the plane
    tolA = near * np.linalg.norm(nB, axis=1)[:, None]
    tolB = near * np.linalg.norm(nA, axis=1)[:, None]
    dA = np.where(np.abs(dA) <= tolA, 0.0, dA)
    dB = np.where(np.abs(dB) <= tolB, 0.0, dB)
    straddle = ((dA.max(axis=1) > 0) & (dA.min(axis=1) < 0)
                & (dB.max(axis=1) > 0) & (dB.min(axis=1) < 0))
    out = np.zeros(len(A), dtype=bool)
    if not straddle.any():
        return out
    k = np.flatnonzero(straddle)
    D = np.cross(nA[k], nB[k])
    dn = np.linalg.norm(D, axis=1)
    ok = dn > PARALLEL * np.linalg.norm(nA[k], axis=1) * np.linalg.norm(nB[k], axis=1)
    k, D, dn = k[ok], D[ok], dn[ok]
    if not len(k):
        return out
    D = D / dn[:, None]
    loA, hiA = _interval(np.einsum("kij,kj->ki", A[k], D), dA[k])
    loB, hiB = _interval(np.einsum("kij,kj->ki", B[k], D), dB[k])
    overlap = np.minimum(hiA, hiB) - np.maximum(loA, loB)
    out[k] = overlap > near[k, 0]
    return out


def _interval(p: np.ndarray, d: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Where a triangle cuts the planes' common line: its corners projected on the line (p) and
    their signed distances to the other plane (d), per pair -> (low, high) along the line."""
    vals = []
    for i, j in ((0, 1), (1, 2), (2, 0)):
        di, dj = d[:, i], d[:, j]
        cross = di * dj < 0
        t = np.where(cross, di / np.where(cross, di - dj, 1.0), np.nan)
        vals.append(np.where(cross, p[:, i] + (p[:, j] - p[:, i]) * t, np.nan))
    for i in range(3):
        vals.append(np.where(d[:, i] == 0.0, p[:, i], np.nan))
    v = np.stack(vals, axis=1)
    return np.nanmin(v, axis=1), np.nanmax(v, axis=1)


#: Winding-number evaluations (sample points x triangles) the nesting search may spend.
MAX_WINDING_WORK = 400_000_000
#: Sample points per body: just inside the centres of its largest triangles, by NEST_DEPTH of the
#: body's size - inside its own material, so a point is inside another body only if they share it.
NEST_SAMPLES = 16
NEST_DEPTH = 1e-5


@dataclass(frozen=True)
class Nesting:
    """Bodies whose surface lies inside another body's material without crossing it: (body, the
    body it sits in, the sample point), and whether every body was looked at."""

    found: tuple[tuple[int, int, tuple[float, float, float]], ...]
    complete: bool


def nested_bodies(points: np.ndarray, triangles: np.ndarray, body: np.ndarray,
                  inward: np.ndarray, *, max_work: int = MAX_WINDING_WORK) -> Nesting:
    """Overlaps a crossing search cannot see: a body wholly inside another's material, or two that
    share faces in one plane while one runs into the other (boxes overlapping with matching
    sides). Read by the generalised winding number (the solid angle a closed surface fills, over
    4 pi) of a few points just inside each body, counted from every OTHER body: 0 is open space
    (or a cavity), 1 is inside material. An outward body's points must read 0; a cavity's
    (`inward` True: its points sit in the material round it) must read 1. A body touching another
    face to face keeps its points on its own side, so touching is never an overlap. `body` (m,) gives each triangle's closed body, -1 for
    none; `inward` (n_bodies,)."""
    P = np.asarray(points, dtype=np.float64)
    T = np.asarray(triangles, dtype=np.int64)
    body = np.asarray(body, dtype=np.int64)
    ids = np.unique(body[body >= 0])
    if len(ids) < 2:
        return Nesting((), True)
    C = P[T]
    order = np.argsort(body, kind="stable")
    starts = np.searchsorted(body[order], ids, side="left")
    ends = np.searchsorted(body[order], ids, side="right")
    members = {int(k): order[s:e] for k, s, e in zip(ids.tolist(), starts.tolist(), ends.tolist())}
    blo = np.array([C[members[k]].reshape(-1, 3).min(axis=0) for k in ids.tolist()])
    bhi = np.array([C[members[k]].reshape(-1, 3).max(axis=0) for k in ids.tolist()])
    area = np.linalg.norm(np.cross(C[:, 1] - C[:, 0], C[:, 2] - C[:, 0]), axis=1)
    found: list = []
    work = 0
    for n, k in enumerate(ids.tolist()):
        mine = members[k]
        top = mine[np.argsort(-area[mine], kind="stable")[:NEST_SAMPLES]]
        nrm = np.cross(C[top, 1] - C[top, 0], C[top, 2] - C[top, 0])
        nrm /= np.maximum(np.linalg.norm(nrm, axis=1), 1e-300)[:, None]
        q = C[top].mean(axis=1) - NEST_DEPTH * float(np.linalg.norm(bhi[n] - blo[n])) * nrm
        held = np.flatnonzero(np.all(blo <= q.max(axis=0), axis=1) & np.all(bhi >= q.min(axis=0), axis=1))
        held = held[held != n]
        if not len(held):
            continue
        total = np.zeros(len(q))
        best, best_w = -1, 0.0
        for j in held.tolist():
            tj = members[int(ids[j])]
            work += len(q) * len(tj)
            if work > max_work:
                return Nesting(tuple(found), False)
            w = _winding(q, C[tj])
            total += w
            if float(np.abs(w).max()) > best_w:
                best, best_w = int(ids[j]), float(np.abs(w).max())
        expect = 1.0 if bool(np.asarray(inward)[k]) else 0.0
        bad = np.flatnonzero(total > expect + 0.75)
        if len(bad):
            found.append((k, best, tuple(float(v) for v in q[bad[0]])))
    return Nesting(tuple(found), True)


def _winding(q: np.ndarray, tri: np.ndarray, chunk: int = 200_000) -> np.ndarray:
    """The generalised winding number of points q (s, 3) about triangles tri (n, 3, 3)."""
    out = np.zeros(len(q))
    for s in range(0, len(tri), chunk):
        t = tri[s:s + chunk]
        a = t[None, :, 0] - q[:, None]
        b = t[None, :, 1] - q[:, None]
        c = t[None, :, 2] - q[:, None]
        la, lb, lc = (np.linalg.norm(v, axis=2) for v in (a, b, c))
        det = np.einsum("sni,sni->sn", a, np.cross(b, c))
        den = (la * lb * lc + np.einsum("sni,sni->sn", a, b) * lc
               + np.einsum("sni,sni->sn", a, c) * lb + np.einsum("sni,sni->sn", b, c) * la)
        out += 2.0 * np.arctan2(det, den).sum(axis=1)
    return out / (4.0 * math.pi)
