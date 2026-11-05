# Responsibility: Read a closed triangle surface as a LUMEN - the fluid inside a network of tubes (an
# artery tree, the water in a coiled pipe) exported as the closed volume a solver reads - and find the
# tubes' ends, which are its openings whether they are closed by flat lids or by smooth, rounded caps.
# Owns: what a tube end is, how tube-like a body is, and when that evidence says "lumen".
# Boundaries: numpy and scipy over triangles, in the surface's own units. No OpenCASCADE, no model, no
# storage, and nothing about what the part is called: every rule is measured on the triangles.
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

# WHAT A LUMEN IS. Walk along the skin from one end of a tube network (the geodesic distance from the
# end farthest from anywhere): every band of equal distance is a RING round a tube - its points sit
# about one radius from the ring's centre, measured square to the way the rings advance. Where the
# distance stops growing, a tube ENDS - flat lid or rounded cap alike. So:
#  - a TUBE section is a round ring (its radius varies little round it);
#  - a tube END is a local maximum of the distance (or the walk's own start), on a tube that runs
#    back at least END_LENGTH of its radius before it meets another branch: a bump on a wall is no end;
#  - a LUMEN is a body whose skin is mostly round rings, whose tubes are thin against the whole part
#    and long against their own width, and which BRANCHES or WINDS. A straight capsule is as likely a
#    rod in a crossflow as the water in a pipe, so it is left to the user - its ends are still found.

#: A ring is round when its points' distance from its centre varies by at most this share of the mean;
#: the tube behind an end may be a little less so (a band cuts a tight bend obliquely).
ROUND_CV = 0.25
END_ROUND_CV = 0.35
#: A ring goes all the way round its centre: its points' unit directions from the centre average to
#: at most this (0 for a full circle, about 0.64 for a half one).
ENCIRCLED = 0.3
#: The bands are BAND_OF_RADIUS of the body's mean tube radius (2 x volume / area) wide, and at least
#: BAND_OF_EDGE of the skin's median edge, so a ring holds a connected loop of points.
BAND_OF_RADIUS = 0.4
BAND_OF_EDGE = 1.5
#: Where the rings fork, a branch is real when the skin beyond it is at least this many times the
#: area of a disc of the tube's radius; a ring that splits round a dent and joins again is not.
FORK_AREA = 2.0
#: ...and never more than this many along one body: a body this long against its tubes is read coarser.
MAX_BANDS = 3000
#: A tube end's tube runs back at least this many of its radii before it meets a branch.
END_LENGTH = 2.0
#: The tube is measured past the end's own cap - a rounded cap runs a quarter turn (pi/2 radii) along
#: the skin - over the next TUBE_SPAN radii.
DOME = 1.6
TUBE_SPAN = 2.0
#: ...and its width measured across it by ACROSS_RAYS rays square to its axis, ACROSS_DEPTHS radii in
#: from the end's apex (past a rounded cap's quarter turn).
ACROSS_DEPTHS = (1.5, 2.5)
ACROSS_RAYS = 8
ACROSS_TRIES = 6
#: A body with more candidate ends than this is not a tube network (a rough blob's bumps): not read.
MAX_TIPS = 4000
#: THE LUMEN TEST: round rings cover this share of the skin, the median tube radius is at most this
#: share of the part's diagonal, the skeleton is at least SLENDER tube widths long, and a two-ended
#: tube's path is at least WINDING times the straight line between its ends (or it has 3+ ends).
TUBE_SHARE = 0.7
THIN_TUBE = 0.04
SLENDER = 10.0
WINDING = 1.3
#: A tube end is at least this many of the skin's median edges across in radius: a thinner one is a
#: spike of the skin a few triangles wide, not a vessel a mesh can give an opening to.
RESOLVED_END = 0.6
#: A component smaller than this share of the skin is debris, not a tube of the network.
MIN_PIECE_SHARE = 0.01
#: The skin's typical facet (its longest side, the median by area) is at most this many of the
#: body's mean tube radius: finer, and the rings round its tubes can be read from its points.
RESOLVED = 1.5


@dataclass
class TubeEnd:
    """One end of a tube, in the surface's units. `apex` is the end's farthest point on the skin (on
    the lid for a flat end, on the cap for a rounded one); `normal` runs along the tube, out of the
    part; `radius` is the tube's one radius back from the end; `length` how far the tube runs back to
    the branch it leaves (or to the network's other end); `roundness` the ring's radius spread."""

    apex: np.ndarray
    normal: np.ndarray
    radius: float
    length: float
    roundness: float
    ring_centre: np.ndarray

    @property
    def diameter(self) -> float:
        return 2.0 * self.radius


@dataclass
class LumenReading:
    ends: list[TubeEnd] = field(default_factory=list)
    tube_share: float = 0.0          # share of the skin on round rings
    radius: float = 0.0              # the tubes' median radius (by skin area)
    length: float = 0.0              # the skeleton's total length
    winding: float = 1.0             # path / straight line, for a network with two ends
    diag: float = 0.0
    lumen: bool = False
    why: str = ""
    #: the local ends that were not tube ends, and why (too short a tube, no round ring behind them)
    dropped: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"ends": len(self.ends), "tube_share": round(self.tube_share, 3),
                "radius_over_diag": round(self.radius / self.diag, 4) if self.diag else None,
                "slender": round(self.length / max(2.0 * self.radius, 1e-30), 1),
                "winding": round(self.winding, 2), "lumen": self.lumen, "why": self.why}


def read_lumen(mesh) -> LumenReading | None:
    """The tube-network reading of a closed surface (cad/open_ends._Mesh), or None when it has no
    closed piece to read or is too rough to be a tube network."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components, dijkstra

    V, F = mesh.verts, mesh.faces
    if len(F) < 16 or not bool(np.all(mesh.count == 2)):
        return None                                   # only a closed skin bounds a lumen
    nv = len(V)
    ea, eb = mesh.ea, mesh.eb
    w = np.linalg.norm(V[ea] - V[eb], axis=1)
    G = coo_matrix((w, (ea, eb)), shape=(nv, nv)).tocsr()
    _, comp = connected_components(G, directed=False)

    # each closed piece's size: its tubes' mean radius is 2 x volume / area (a tube's V/A is r/2)
    fcomp = comp[F[:, 0]]
    n_comp = int(comp.max()) + 1
    area_c = np.bincount(fcomp, weights=mesh.area, minlength=n_comp)
    ref = V.mean(axis=0)
    a, b, c = (V[F[:, k]] - ref for k in range(3))
    vol_c = np.bincount(fcomp, weights=np.einsum("ij,ij->i", a, np.cross(b, c)) / 6.0, minlength=n_comp)
    total = float(area_c.sum()) or 1.0
    pieces = np.flatnonzero((area_c >= MIN_PIECE_SHARE * total) & (vol_c > 0))
    if len(pieces) == 0:
        return None
    r_mean = np.where(area_c > 0, 2.0 * np.abs(vol_c) / np.where(area_c > 0, area_c, 1.0), 0.0)
    # THE SKIN MUST RESOLVE ITS TUBES: rings are read from the skin's points, so a skin drawn in long
    # strips (an extruded wing's two-triangle span, a CAD cylinder's full-length facets) has none to read
    tri = np.stack([V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 1]], V[F[:, 0]] - V[F[:, 2]]], axis=1)
    longest = np.linalg.norm(tri, axis=2).max(axis=1)
    o = np.argsort(longest)
    cum = np.cumsum(mesh.area[o])
    typical = float(longest[o][np.searchsorted(cum, 0.5 * cum[-1])])
    r_body = 2.0 * float(np.abs(vol_c[pieces]).sum()) / max(float(area_c[pieces].sum()), 1e-300)
    if typical > RESOLVED * r_body:
        return None

    # THE WALK: from any point of each piece to its farthest (an end), then from there
    vsel = np.isin(comp, pieces)
    first = np.array([int(np.flatnonzero(comp == p)[0]) for p in pieces])
    d0 = dijkstra(G, directed=False, indices=first, min_only=True)
    roots = []
    for p in pieces:
        idx = np.flatnonzero(comp == p)
        roots.append(int(idx[np.argmax(d0[idx])]))
    d = dijkstra(G, directed=False, indices=np.asarray(roots), min_only=True)
    d = np.where(vsel & np.isfinite(d), d, -1.0)
    span = np.zeros(n_comp)
    np.maximum.at(span, comp[vsel], d[vsel])
    # the bands BAND_OF_RADIUS of the tubes' mean radius wide - but wide enough for a ring to hold a
    # connected loop of the skin's points, and no more than MAX_BANDS along a piece
    edge = float(np.median(w)) if len(w) else 0.0
    h = np.maximum(BAND_OF_RADIUS * r_mean, BAND_OF_EDGE * edge)
    h = np.maximum(h, span / MAX_BANDS)
    h = np.where(h > 0, h, 1.0)
    band = np.where(vsel, np.floor(d / h[comp]).astype(np.int64), -1)

    # THE RINGS: each band of each piece, split into its connected parts
    same = vsel[ea] & (band[ea] == band[eb])
    S = coo_matrix((np.ones(int(same.sum())), (ea[same], eb[same])), shape=(nv, nv))
    _, lab = connected_components(S, directed=False)
    n_lab = int(lab.max()) + 1
    # each skin point's share of the area, for the rings' areas and their weighted radii
    va = np.bincount(F.reshape(-1), weights=np.repeat(mesh.area / 3.0, 3), minlength=nv)
    cnt = np.bincount(lab, weights=vsel.astype(float), minlength=n_lab)
    good = cnt > 0
    C = np.stack([np.bincount(lab, weights=V[:, k] * vsel, minlength=n_lab) for k in range(3)], axis=1) \
        / np.where(good, cnt, 1.0)[:, None]
    dmean = np.bincount(lab, weights=np.where(vsel, d, 0.0), minlength=n_lab) / np.where(good, cnt, 1.0)
    order = np.lexsort((d, lab))
    last = np.r_[np.flatnonzero(np.diff(lab[order])), len(order) - 1]
    top = np.full(n_lab, -1, dtype=np.int64)
    top[lab[order[last]]] = order[last]              # each ring's farthest point
    area_l = np.bincount(lab, weights=va * vsel, minlength=n_lab)

    # how the rings follow one another: a step of one band, from the nearer ring to the farther
    up = vsel[ea] & vsel[eb] & (np.abs(band[ea] - band[eb]) == 1)
    lo = np.where(band[ea[up]] < band[eb[up]], lab[ea[up]], lab[eb[up]])
    hi = np.where(band[ea[up]] < band[eb[up]], lab[eb[up]], lab[ea[up]])
    pairs = np.unique(np.stack([lo, hi], axis=1), axis=0) if len(lo) else np.zeros((0, 2), dtype=np.int64)
    lo, hi = pairs[:, 0], pairs[:, 1]
    n_succ = np.bincount(lo, minlength=n_lab)
    n_pred = np.bincount(hi, minlength=n_lab)
    succ_c = np.stack([np.bincount(lo, weights=C[hi, k], minlength=n_lab) for k in range(3)], axis=1)
    pred_c = np.stack([np.bincount(hi, weights=C[lo, k], minlength=n_lab) for k in range(3)], axis=1)
    fwd = np.where(n_succ[:, None] > 0, succ_c / np.maximum(n_succ, 1)[:, None], C)
    back = np.where(n_pred[:, None] > 0, pred_c / np.maximum(n_pred, 1)[:, None], C)
    t = fwd - back
    tl = np.linalg.norm(t, axis=1)
    t = t / np.where(tl > 0, tl, 1.0)[:, None]

    # EACH RING'S RADIUS, square to the way the rings advance, and how round it is
    rel = V - C[lab]
    along = np.einsum("ij,ij->i", rel, t[lab])
    radial = rel - along[:, None] * t[lab]
    rad = np.linalg.norm(radial, axis=1)
    wv = va * vsel
    wsum = np.where(area_l > 0, area_l, 1.0)
    R = np.bincount(lab, weights=rad * wv, minlength=n_lab) / wsum
    R2 = np.bincount(lab, weights=rad * rad * wv, minlength=n_lab) / wsum
    cv = np.sqrt(np.maximum(R2 - R * R, 0.0)) / np.where(R > 0, R, 1.0)
    # ...and whether it goes all the way round its centre: the points' directions from the centre
    # cancel out for a ring, and do not for an arc across a flat side (a wing's level lines)
    unit = radial / np.where(rad > 0, rad, 1.0)[:, None]
    res = np.linalg.norm(np.stack([np.bincount(lab, weights=unit[:, k] * wv, minlength=n_lab)
                                   for k in range(3)], axis=1), axis=1) / wsum
    cv = np.where(res <= ENCIRCLED, cv, np.inf)       # an arc is no ring at all
    round_ring = good & (cnt >= 6) & (cv <= ROUND_CV) & (tl > 0)

    reading = LumenReading(diag=float(mesh.diag))
    skin = float(area_l[good].sum()) or 1.0
    reading.tube_share = float(area_l[round_ring].sum()) / skin
    if round_ring.any():
        o = np.argsort(R[round_ring])
        cum = np.cumsum(area_l[round_ring][o])
        reading.radius = float(R[round_ring][o][np.searchsorted(cum, 0.5 * cum[-1])])
    reading.length = float(np.linalg.norm(C[hi] - C[lo], axis=1).sum()) if len(lo) else 0.0

    # THE ENDS: every ring nothing follows (a local maximum of the distance), and each walk's start
    leaves = np.flatnonzero(good & (n_succ == 0))
    starts = np.unique(lab[np.asarray(roots)])
    if len(leaves) + len(starts) > MAX_TIPS:
        reading.why = f"{len(leaves)} local ends: too rough to be a tube network"
        return reading
    # the skin beyond each ring (everything farther along the walk): what tells a real branch from a
    # ring that split round a dent
    lab_band = np.full(n_lab, -1, dtype=np.int64)
    np.maximum.at(lab_band, lab[vsel], band[vsel])
    sub = area_l.copy()
    by_band = np.argsort(-lab_band[lo], kind="stable")
    lo_s, hi_s, b_s = lo[by_band], hi[by_band], lab_band[lo][by_band]
    cuts = np.r_[0, np.flatnonzero(np.diff(b_s)) + 1, len(b_s)]
    for i in range(len(cuts) - 1):
        s = slice(cuts[i], cuts[i + 1])
        np.add.at(sub, lo_s[s], sub[hi_s[s]])
    succs = _lists(lo, hi, n_lab)
    preds = _lists(hi, lo, n_lab)
    root_of = {int(lab[r]): int(r) for r in roots}
    ends: list[TubeEnd] = []
    for L in np.r_[leaves, starts].tolist():
        downhill = n_succ[L] == 0
        tip = int(top[L]) if downhill else root_of.get(int(L), -1)
        if tip < 0:
            continue
        d_tip = float(d[tip])
        cur, came = L, -1
        widest = float(h[comp[tip]])
        walked: list[tuple[float, int]] = []            # (distance back from the end, ring)
        for _ in range(n_lab):
            # the tube's radius as far as the walk has seen it (a rounded end's first rings are small)
            widest = max(widest, float(R[cur]))
            big = FORK_AREA * math.pi * widest * widest
            if downhill:
                # a branch joins here when a ring beyond this one, other than the way we came, leads on
                # to real skin
                fork = cur != L and any(sub[s] >= big for s in succs[cur] if s != came)
                nxt = max(preds[cur], key=lambda p: area_l[p], default=-1)
            else:
                ahead = sorted((sub[s] for s in succs[cur]), reverse=True)
                fork = len(ahead) >= 2 and ahead[1] >= big
                nxt = max(succs[cur], key=lambda s: sub[s], default=-1)
            if fork:
                break                                    # the branch this tube leaves
            walked.append((abs(d_tip - float(dmean[cur])), int(cur)))
            if nxt < 0:
                break
            cur, came = int(nxt), cur
        end = _tube_behind(walked, R, cv, C)
        length = walked[-1][0] if walked else 0.0
        if end is None or length < END_LENGTH * end[0]:
            reading.dropped.append({"at": V[tip].round(6).tolist(), "length": round(length, 6),
                                    "radius": round(end[0], 6) if end else None,
                                    "roundness": round(end[1], 3) if end else None, "why": "rings"})
            continue
        radius, roundness, centre = end
        n = V[tip] - centre
        nl = float(np.linalg.norm(n))
        if nl <= 0:
            continue
        n = n / nl
        # THE TUBE'S WIDTH, MEASURED ACROSS IT: rays square to the axis from points on it behind the
        # cap meet the wall one radius away all round - truer than a ring a bend cuts obliquely, and
        # a ray that escapes says the end closes no tube at all
        tube = _across(mesh, V[tip], n, radius)
        across = tube[0] if tube is not None else None
        if tube is None or tube[0] < RESOLVED_END * edge or length < END_LENGTH * tube[0]:
            reading.dropped.append({"at": V[tip].round(6).tolist(), "length": round(length, 6),
                                    "radius": round(radius, 6), "roundness": round(roundness, 3),
                                    "why": "across" if across is None else f"thin {across:.3g}"})
            continue
        ends.append(TubeEnd(apex=tube[2], normal=tube[1], radius=tube[0], length=length,
                            roundness=roundness, ring_centre=centre))
    reading.ends = _distinct(ends)

    # THE VERDICT
    n_ends = len(reading.ends)
    if n_ends == 2 and len(pieces) == 1:
        e0, e1 = reading.ends
        chord = float(np.linalg.norm(e0.apex - e1.apex))
        reading.winding = (float(d.max()) / chord) if chord > 0 else 1.0
    thin = reading.radius > 0 and reading.radius <= THIN_TUBE * reading.diag
    slender = reading.length >= SLENDER * 2.0 * reading.radius > 0
    shaped = n_ends >= 3 or reading.winding >= WINDING
    reading.lumen = bool(n_ends >= 2 and reading.tube_share >= TUBE_SHARE and thin and slender and shaped)
    if reading.lumen:
        reading.why = (f"a network of round tubes ({100 * reading.tube_share:.0f}% of its skin) with {n_ends} ends, "
                       f"its tubes {2 * reading.radius / reading.diag * 100:.1f}% of its size across")
    else:
        missing = [w for ok, w in ((n_ends >= 2, "two tube ends"), (reading.tube_share >= TUBE_SHARE, "round tubes"),
                                   (thin, "thin tubes"), (slender, "a long network"), (shaped, "branches or bends"))
                   if not ok]
        reading.why = "not a lumen: no " + ", no ".join(missing)
    return reading


def _tube_behind(walked: list[tuple[float, int]], R, cv, C) -> tuple[float, float, np.ndarray] | None:
    """The tube behind an end, from the rings walked back from it: past the end's own cap (a rounded
    cap is a quarter turn, DOME radii of the widest ring seen; a flat lid one radius), the rings over
    the next TUBE_SPAN radii - their median radius and roundness, and their centre. None when the walk
    never got past the cap."""
    widest = 0.0
    for k, (s, lab) in enumerate(walked):
        widest = max(widest, float(R[lab]))
        if widest > 0 and s >= DOME * widest:
            rings = [lb for t, lb in walked[k:] if t <= s + TUBE_SPAN * widest]
            radius = float(np.median(R[rings]))
            return radius, float(np.median(cv[rings])), C[rings].mean(axis=0)
    return None


def _across(mesh, apex: np.ndarray, n: np.ndarray, radius: float) -> tuple[float, np.ndarray, np.ndarray] | None:
    """The tube behind an end, by rays: (its radius, its axis out of the part, where that axis comes
    out through the end), or None when it is no
    round tube. From points on the axis ACROSS_DEPTHS radii in from the apex, ACROSS_RAYS rays square
    to it meet the wall; the rings only estimate where the axis runs and how wide the tube is, so the
    points are moved onto the middle of what the rays met, the axis turned through those middles and
    the depths re-scaled by the radius they measured, until the two agree. A ray that meets nothing
    (the end closes no tube), or rays that disagree by more than END_ROUND_CV, mean no round tube."""
    angles = np.linspace(0.0, 2.0 * math.pi, ACROSS_RAYS, endpoint=False)
    r, axis, base = radius, n, apex
    for _ in range(ACROSS_TRIES):
        u = np.cross(axis, [0.0, 0.0, 1.0] if abs(axis[2]) < 0.9 else [0.0, 1.0, 0.0])
        u /= np.linalg.norm(u)
        w = np.cross(axis, u)
        dirs = np.asarray([math.cos(a) * u + math.sin(a) * w for a in angles])
        near = mesh._near(apex, (max(ACROSS_DEPTHS) + 4.0) * r)
        middles, widths, spread = [], [], 0.0
        for k in ACROSS_DEPTHS:
            o = base - axis * (k * r)
            hits = mesh._hits(np.repeat(o[None, :], len(dirs), axis=0), dirs, near)
            if not np.all(np.isfinite(hits)):
                return None
            middles.append((o + hits[:, None] * dirs).mean(axis=0))
            widths.append(float(np.median(hits)))
            spread = max(spread, float(np.std(hits)) / max(float(np.mean(hits)), 1e-300))
        measured = float(np.median(widths))
        turn = middles[0] - middles[1]
        tl = float(np.linalg.norm(turn))
        new_axis = turn / tl if tl > 0 else axis
        settled = abs(measured - r) <= 0.1 * measured and float(new_axis @ axis) >= 0.99
        if settled:
            if spread > END_ROUND_CV:
                return None
            # where the tube's axis comes out through its end: the opening's centre on the cap (the
            # farthest point of the walk sits off it where the tube bends)
            out = mesh._hits(middles[1][None, :], new_axis[None, :], near)[0]
            tip = middles[1] + new_axis * out if np.isfinite(out) else apex
            return measured, new_axis, tip
        r, axis = measured, new_axis
        base = middles[0] + axis * float((apex - middles[0]) @ axis)
    return None


def _lists(frm: np.ndarray, to: np.ndarray, n: int) -> list[list[int]]:
    """For each of n nodes, the nodes the pairs (frm -> to) lead to."""
    out: list[list[int]] = [[] for _ in range(n)]
    for a, b in zip(frm.tolist(), to.tolist()):
        out[a].append(b)
    return out


def _distinct(ends: list[TubeEnd]) -> list[TubeEnd]:
    """One end per tube: two found within one radius of each other (a lid's two corners both reading
    as the farthest point) are one, the longer tube's."""
    ends = sorted(ends, key=lambda e: e.length, reverse=True)
    kept: list[TubeEnd] = []
    for e in ends:
        if all(float(np.linalg.norm(e.apex - k.apex)) > max(e.radius, k.radius) for k in kept):
            kept.append(e)
    return kept


__all__ = ["LumenReading", "TubeEnd", "read_lumen"]
