# Responsibility: The STRUCTURED steps of the shell mesh - what makes it look like an engineer's hand-made mesh:
# WASHERS round holes (SHMESH-39: a concentric ring of quads hugging each round hole of a flat face, an even number of
# elements round it, the field mesh growing out from the ring) and MAPPED quads on faces with four natural corners
# (SHMESH-40: a transfinite grid, opposite sides counted alike, pipes as a grid of rows along the axis and columns
# round it). Everything here is planned from the face's own geometry and the sizes the plan already chose.
# Boundaries: works on the gmsh model the shell adapter built (engines/gmsh/shell_adapter.py), in that process;
# reads and writes nothing else. Deterministic: every choice is made in a fixed order with fixed numbers.
# Collaborates with: engines/gmsh/shell_adapter.py (calls it before and after the 2D mesh), engines/shell_structure.py
# (measures the result).
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

#: SHMESH-39: washers round the round holes of flat faces
WASHERS = True
#: a hole gets a washer when it is at most WASHER_ACROSS times its face's element size across (a bigger hole is a
#: cut-out: the field mesh already follows it)
WASHER_ACROSS = 3.0
#: SHMESH-45e: ... and up to WASHER_ACROSS_USER sizes across under the user's own global size (a 46 mm hole at 10 mm:
#: an engineer puts a washer there; the rows then stay inside the user's shortest and longest edge). Kt of a plate with
#: a 20 mm hole at 5 mm (4 sizes across): 2.34 with a washer, 2.36 without, theory 2.51 - the same within 1%.
WASHER_ACROSS_USER = 6.0
#: the elements round a hole: a multiple of WASHER_STEP, at least WASHER_MIN (8, 12, 16 ... as an engineer counts),
#: never fewer than the plan put round it (the hole keeps its shape), at most WASHER_MAX
WASHER_MIN = 8
WASHER_STEP = 4
WASHER_MAX = 36
#: the ring's outer diameter is WASHER_RING times the hole's (an engineer's washer: 1.5 to 2 times); its rows are
#: near-square (each row's width its arc length round), at most WASHER_ROWS rows
WASHER_RING = (1.4, 2.0)
WASHER_ROWS = 2
#: a ring squeezed by an edge or another hole may shrink to WASHER_SHRUNK times the hole's diameter; past that the hole
#: has no washer
WASHER_SHRUNK = 1.25
#: between a ring and any other edge of its face at least WASHER_ROOM of the ring's outer element length is left for
#: the field mesh
WASHER_ROOM = 0.5
#: a hole's circle: its points within CIRCLE_TOL of its radius from its centre
CIRCLE_TOL = 1e-3
#: SHMESH-40: faces with four natural corners meshed as mapped grids
MAPPED = True


@dataclass
class Washer:
    """One planned washer (lengths in model units)."""
    face: int                       # the gmsh surface holding the hole (before the imprint)
    owner: int                      # its sewn face (0-based)
    hole: list[int]                 # the hole's curves (before the imprint)
    centre: np.ndarray
    normal: np.ndarray
    xaxis: np.ndarray               # from the centre to the hole's first point (the ring starts there too)
    r: float
    R: float
    n: int                          # elements round
    rows: int
    out_mm: float                   # the ring's outer element length (mm)
    shrunk: bool = False
    corners: list = field(default_factory=list)  # SHMESH-45f: a square (four-sided) hole's corners, in loop order
    side_counts: list = field(default_factory=list)
    ring: int = 0                   # after the imprint: the ring's circle
    annulus: int = 0                # ... the surface between hole and ring
    hole_after: list[int] = field(default_factory=list)
    info: dict = field(default_factory=dict)  # SHMESH-45c: the hole's line in the record (quality.structure.hole_list)


def _samples(gmsh, curve: int, n: int = 24) -> np.ndarray:
    lo, hi = gmsh.model.getParametrizationBounds(1, curve)
    ts = np.linspace(lo[0], hi[0], n + 1)
    return np.asarray(gmsh.model.getValue(1, curve, ts.tolist()), dtype=float).reshape(-1, 3)


def _circle_of(gmsh, curves: list[int]) -> tuple[np.ndarray, float, np.ndarray, np.ndarray] | None:
    """(centre, radius, normal, first point) when the loop's curves together are one circle, else None."""
    if not curves:
        return None
    pts = [_samples(gmsh, c) for c in curves]
    allp = np.vstack(pts)
    # the centre: least squares in the loop's own plane
    c0 = allp.mean(axis=0)
    _u, _s, vt = np.linalg.svd(allp - c0, full_matrices=False)
    n = vt[2]
    e1, e2 = vt[0], vt[1]
    xy = np.column_stack([(allp - c0) @ e1, (allp - c0) @ e2])
    A = np.column_stack([2 * xy, np.ones(len(xy))])
    sol, *_r = np.linalg.lstsq(A, (xy ** 2).sum(axis=1), rcond=None)
    centre = c0 + sol[0] * e1 + sol[1] * e2
    rad = np.linalg.norm(allp - centre, axis=1)
    r = float(rad.mean())
    if r <= 0 or float(np.abs(rad - r).max()) > CIRCLE_TOL * r or float(np.abs((allp - centre) @ n).max()) > CIRCLE_TOL * r:
        return None
    # the loop must go once round (the points' angles cover the whole turn)
    ang = np.sort(np.arctan2(xy[:, 1] - sol[1], xy[:, 0] - sol[0]))
    gaps = np.diff(np.concatenate([ang, ang[:1] + 2 * math.pi]))
    if float(gaps.max()) > 0.5 * math.pi:
        return None
    return centre, r, n, pts[0][0]


def _count(r_mm: float, hole_mm: float, face_mm: float, low_mm: float | None = None,
           high_mm: float | None = None) -> tuple[int, int, float] | None:
    """SHMESH-39: (elements round, rows, ring radius over the hole's) for a hole of radius r_mm whose plan puts
    elements of hole_mm round it in a face of face_mm elements: the fewest elements round (a multiple of WASHER_STEP,
    at least WASHER_MIN and the plan's own count) with near-square rows whose outer edge (WASHER_RING times the hole,
    narrower when its elements would come out longer than the face's) has elements no longer than the face's; None
    when no count up to WASHER_MAX does it."""
    own = math.ceil(2 * math.pi * r_mm / max(hole_mm, 1e-12) - 1e-9)
    n0 = max(WASHER_MIN, WASHER_STEP * math.ceil(own / WASHER_STEP))
    for n in range(n0, WASHER_MAX + 1, WASHER_STEP):
        q = math.exp(2 * math.pi / n)
        if low_mm is not None and n > n0 and 2 * math.pi * r_mm / n < low_mm:
            break                      # SHMESH-45e: never an edge round the hole under the user's shortest edge
        for rows in range(1, WASHER_ROWS + 1):
            if q ** rows < WASHER_RING[0]:
                continue
            # as wide as square rows make it, but no wider than the face's size allows round its outer edge and
            # across each row
            ratio = min(q ** rows, WASHER_RING[1], face_mm * n / (2 * math.pi * r_mm), 1 + rows * face_mm / r_mm)
            if high_mm is not None:
                ratio = min(ratio, 0.98 * high_mm * n / (2 * math.pi * r_mm), 1 + rows * 0.98 * high_mm / r_mm)
            if low_mm is not None and (ratio - 1) * r_mm / rows < low_mm:
                if rows < WASHER_ROWS:
                    continue
                break
            if ratio >= WASHER_RING[0]:
                return n, rows, ratio
            break
    return None


def _loop_kind(gmsh, curves: list[int]) -> str:
    """SHMESH-45c: what an inner loop that is not one circle is, in a few words: a slot (two straight sides and two
    round ends), an ellipse, a polygon, a circle in pieces of different radius, or its curve types."""
    types = [gmsh.model.getType(1, c) for c in curves]
    lines = sum(1 for x in types if x == "Line")
    arcs = [c for c, x in zip(curves, types, strict=True) if x == "Circle"]
    if any(x == "Ellipse" for x in types):
        return "an ellipse" if lines == 0 else "a loop with elliptic arcs"
    if lines == len(curves):
        return f"a polygon of {lines} straight edges"
    if lines == 2 and len(arcs) == 2:
        return "a slot (two straight sides, two round ends)"
    if arcs and len(arcs) == len(curves):
        radii = []
        for c in arcs:
            got = _circle_of(gmsh, [c]) if False else None
            pts = _samples(gmsh, c, 8)
            mid = pts.mean(axis=0)
            radii.append(float(np.linalg.norm(pts - mid, axis=1).mean()) if got is None else got[1])
        if max(radii) > 1.001 * min(radii):
            return f"a circle in {len(arcs)} pieces of different radius"
        return f"{len(arcs)} arcs that do not make one circle"
    kinds: dict[str, int] = {}
    for x in types:
        kinds[x] = kinds.get(x, 0) + 1
    return "a loop of " + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))


def _frame_points(w: Washer) -> tuple[list[np.ndarray], list[tuple[np.ndarray, np.ndarray]]]:
    """SHMESH-45f: a square washer's lines - (the ring's outer corners Q_i, the 4 short lines that carry each hole side
    on past its far corner out to the ring). The ring is four strips like a pinwheel, each a rectangle along one hole
    side and on past its corner, every corner square: no cell with a 45 degree corner."""
    P = w.corners
    c = w.centre
    width = w.R - w.r
    d = [(P[(i + 1) % 4] - P[i]) / np.linalg.norm(P[(i + 1) % 4] - P[i]) for i in range(4)]
    nrm = []
    for i in range(4):
        n_i = np.cross(d[i], w.normal)
        if float(n_i @ (0.5 * (P[i] + P[(i + 1) % 4]) - c)) < 0:
            n_i = -n_i
        nrm.append(n_i / np.linalg.norm(n_i))
    Q = []
    for i in range(4):
        A = np.array([[float(nrm[i - 1] @ d[i - 1]), float(nrm[i - 1] @ d[i])],
                      [float(nrm[i] @ d[i - 1]), float(nrm[i] @ d[i])]])
        try:
            st = np.linalg.solve(A, np.array([width, width]))
        except np.linalg.LinAlgError:
            st = np.array([0.0, 0.0])
        Q.append(P[i] + st[0] * d[i - 1] + st[1] * d[i])
    ext = []
    for i in range(4):
        j = (i + 1) % 4
        t = width / max(float(d[i] @ nrm[j]), 1e-9)          # side i carried on past P_j to side j's offset
        ext.append((P[j], P[j] + t * d[i]))
    return Q, ext


def _square_plan(gmsh, curves: list[int], face_mm: float, mm: float, hole_size, owner: int, low_mm, high_mm,
                 across: float):
    """SHMESH-45f: a square washer for a four-sided hole of straight edges (a square or diamond hole, a carriage
    bolt's): each side's count from the plan's size round the hole (2 or more), the ring one row of near-square cells
    (as wide as a side's segment, inside the user's edges and the face's size) - four strips along the sides and four
    square corner cells. A Washer (corners set), or why not (words)."""
    if len(curves) != 4 or any(gmsh.model.getType(1, c) != "Line" for c in curves):
        return "not a four-sided hole of straight edges"
    ends = [_samples(gmsh, c, 1) for c in curves]
    corners = []
    for i in range(4):
        a_, b_ = ends[i], ends[(i + 1) % 4]
        corners.append(min(((float(np.linalg.norm(x - y)), x) for x in a_ for y in b_), key=lambda z: z[0])[1])
    P = np.array(corners)
    c = P.mean(axis=0)
    sides = [float(np.linalg.norm(P[(i + 1) % 4] - P[i])) for i in range(4)]
    n = np.cross(P[1] - P[0], P[2] - P[1])
    if float(np.linalg.norm(n)) <= 0:
        return "a degenerate four-sided hole"
    n /= float(np.linalg.norm(n))
    for i in range(4):
        u1, u2 = P[i - 1] - P[i], P[(i + 1) % 4] - P[i]
        ang = math.degrees(math.acos(max(-1.0, min(1.0, float(u1 @ u2) / float(np.linalg.norm(u1) * np.linalg.norm(u2))))))
        if not (70.0 <= ang <= 110.0) or float(np.cross(P[i] - P[i - 1], P[(i + 1) % 4] - P[i]) @ n) <= 0:
            return "a four-sided hole that is not near square (a corner past 70..110 degrees)"
    if max(sides) > 2.0 * min(sides):
        return "a four-sided hole more than twice as long as wide"
    diag = 2 * float(np.linalg.norm(P - c, axis=1).max())
    if diag * mm > across * face_mm:
        return f"larger than the washer limit ({across:g} element sizes across)"
    r_mean = float(np.linalg.norm(P - c, axis=1).mean())
    h_hole = hole_size(r_mean * mm, owner)
    counts = [max(2, round(x * mm / max(h_hole, 1e-9))) for x in sides]
    seg = float(np.mean([x * mm / m for x, m in zip(sides, counts, strict=True)]))
    width = min(seg, face_mm)
    if high_mm is not None:
        width = min(width, 0.98 * high_mm)
    if low_mm is not None:
        width = max(width, float(low_mm))
    w = Washer(face=0, owner=owner, hole=list(curves), centre=c, normal=n,
               xaxis=(P[0] - c) / np.linalg.norm(P[0] - c), r=r_mean, R=r_mean + width / mm, n=sum(counts), rows=1,
               out_mm=max(seg, width))
    w.corners = [np.asarray(x) for x in P]
    w.side_counts = counts
    return w


def _square_presets(gmsh, w: Washer, curves: list[int], preset: dict[int, int], cuts: set[int] | None) -> None:
    """SHMESH-45f: the counts of a square washer's curves after its face is cut: a hole side and the strip's outer
    side its count (plus the corner's row), every strip end the row; the ring's own lines go into `cuts`."""
    Q, ext = _frame_points(w)
    P = w.corners
    tol = 1e-6 * max(1.0, float(w.R))
    width = w.R - w.r
    mids = [0.5 * (P[i] + P[(i + 1) % 4]) for i in range(4)]

    def on_seg(x, p0, p1) -> bool:
        d_ = p1 - p0
        tt = float((x - p0) @ d_) / max(float(d_ @ d_), 1e-300)
        return -1e-9 <= tt <= 1 + 1e-9 and float(np.linalg.norm(p0 + tt * d_ - x)) <= tol

    for c in curves:
        a_, b_ = _samples(gmsh, c, 1)
        ln = float(np.linalg.norm(b_ - a_))
        hole_side = next((i for i in range(4) if on_seg(a_, P[i], P[(i + 1) % 4])
                          and on_seg(b_, P[i], P[(i + 1) % 4])), None)
        if hole_side is not None:
            preset[c] = w.side_counts[hole_side]
            continue
        ring = any(on_seg(a_, Q[i], Q[(i + 1) % 4]) and on_seg(b_, Q[i], Q[(i + 1) % 4]) for i in range(4))
        short = any(on_seg(a_, p0, p1) and on_seg(b_, p0, p1) for p0, p1 in ext)
        if not (ring or short):
            continue
        if short or abs(ln - width) <= 0.1 * width:
            preset[c] = w.rows
        else:
            near = int(np.argmin([float(np.linalg.norm(0.5 * (a_ + b_) - m)) for m in mids]))
            preset[c] = w.side_counts[near] + w.rows
        if cuts is not None:
            cuts.add(c)


def plan_washers(gmsh, tag_of: dict[int, int], size_mm: dict[int, float], hole_size, mm: float,
                 names: dict[int, tuple[str, str]] | None = None, before: list | None = None,
                 across: float | None = None, low_mm: float | None = None, high_mm: float | None = None
                 ) -> tuple[list[Washer], dict[str, int], list[dict]]:
    """SHMESH-39: the washers to make: every inner loop of a flat face that is one round hole (a circle no other face
    shares), at most WASHER_ACROSS of the face's element size across. `size_mm`: each surface's element size;
    `hole_size(r_mm, owner)`: the element length the plan puts round a hole of that radius. Returns (the washers,
    {why a hole has none: count}, SHMESH-45c: one line per inner loop of a flat face - its face and part (`names`:
    owner -> (mid-face, part)), diameter, CAD edges before (`before`: [(centre, edges)] from the SHMESH-45b redraw)
    and after, whether another face shares its loop, the ring's planned count and width, the room to the nearest face
    edge and other hole (element sizes), and why it has no washer; the washers' lines are filled in further on)."""
    skipped: dict[str, int] = {}
    found: list[Washer] = []
    lines: list[dict] = []

    def no(info: dict, reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1
        info["washer"] = False
        info["why"] = info.get("why") or reason

    for t, k in sorted(tag_of.items()):
        if gmsh.model.getType(2, t) != "Plane":
            continue
        _l, loops = gmsh.model.occ.getCurveLoops(t)
        loops = [[abs(int(c)) for c in cl] for cl in loops]
        if len(loops) < 2:
            continue
        # the outer loop is the longest
        length = [sum(float(gmsh.model.occ.getMass(1, c)) for c in cl) for cl in loops]
        outer = int(np.argmax(length))
        h = size_mm[t]
        rims = [_samples(gmsh, c, 64) for c in loops[outer]]
        here = {}
        for j, cl in enumerate(loops):
            if j != outer:
                pts = np.vstack([_samples(gmsh, c, 16) for c in cl])
                cj = pts.mean(axis=0)
                here[j] = (cj, float(np.linalg.norm(pts - cj, axis=1).mean()))
        for j, cl in enumerate(loops):
            if j == outer:
                continue
            mid, part = (names or {}).get(k, (str(k), ""))
            c0, r0 = here[j]
            edge_gap = min(float(np.linalg.norm(p - c0, axis=1).min()) for p in rims) - r0
            hole_gap = min((float(np.linalg.norm(c - c0)) - r - r0 for jj, (c, r) in here.items() if jj != j),
                           default=math.inf)
            n_before = next((int(e) for cc, e in (before or [])
                             if float(np.linalg.norm(np.asarray(cc, dtype=float) - c0)) <= 0.25 * max(r0, 1e-9)),
                            len(cl))
            info = {"face": mid, "part": part, "diameter_mm": float(f"{2 * r0 * mm:.4g}"),
                    "edges_before": n_before, "edges": len(cl),
                    "shared": any(len(gmsh.model.getAdjacencies(1, c)[0]) != 1 for c in cl),
                    "to_edge": float(f"{edge_gap * mm / h:.3g}"),
                    "to_hole": float(f"{hole_gap * mm / h:.3g}") if math.isfinite(hole_gap) else None}
            lines.append(info)
            if info["shared"]:
                no(info, "shared with another face")
                continue
            got = _circle_of(gmsh, cl)
            if got is None:
                info["loop"] = _loop_kind(gmsh, cl)
                sq = _square_plan(gmsh, cl, size_mm[t], mm, hole_size, k, low_mm, high_mm,
                                  across if across is not None else WASHER_ACROSS)
                if isinstance(sq, str):
                    no(info, f"not round: {sq}")
                    continue
                info.update(planned_round=sum(sq.side_counts), planned_rows=sq.rows,
                            planned_width_mm=float(f"{(sq.R / sq.r - 1) * sq.r * mm:.4g}"), shape="square")
                sq.face, sq.owner, sq.hole, sq.info = t, k, cl, info
                found.append(sq)
                continue
            centre, r, n, first = got
            limit = across if across is not None else WASHER_ACROSS
            if 2 * r * mm > limit * size_mm[t]:
                no(info, f"larger than the washer limit ({limit:g} element sizes across)")
                continue
            # SHMESH-45e: inside the user's shortest and longest edge where a count fits, else as before
            plan = (_count(r * mm, hole_size(r * mm, k), size_mm[t], low_mm, high_mm)
                    or _count(r * mm, hole_size(r * mm, k), size_mm[t]))
            if plan is None:
                no(info, "no count fits")
                continue
            cnt, rows, ratio = plan
            # a hole drawn as many short curves (a faceted circle) carries at least one element on each
            cnt = max(cnt, WASHER_STEP * math.ceil(len(cl) / WASHER_STEP))
            xa = first - centre
            xa = xa - float(xa @ n) * n
            xa /= max(float(np.linalg.norm(xa)), 1e-300)
            info.update(planned_round=cnt, planned_rows=rows, planned_width_mm=float(f"{(ratio - 1) * r * mm:.4g}"))
            found.append(Washer(face=t, owner=k, hole=cl, centre=centre, normal=n, xaxis=xa, r=r, R=ratio * r,
                                n=cnt, rows=rows, out_mm=2 * math.pi * ratio * r * mm / cnt, info=info))
    # room: each ring against the other edges of its face and the other holes' rings
    keep: list[Washer] = []
    for w in found:
        others = [c for c in {abs(int(c)) for _d, c in gmsh.model.getBoundary([(2, w.face)], oriented=False)}
                  if c not in w.hole]
        gap = math.inf
        for c in sorted(others):
            p = _samples(gmsh, c, 64)
            gap = min(gap, float(np.linalg.norm(p - w.centre, axis=1).min()))
        limit = gap - WASHER_ROOM * w.out_mm / mm
        for o in found:
            if o is w or o.face != w.face:
                continue
            d = float(np.linalg.norm(o.centre - w.centre))
            limit = min(limit, d * w.r / (w.r + o.r) - 0.5 * WASHER_ROOM * w.out_mm / mm)
        if limit < w.R:
            if limit < WASHER_SHRUNK * w.r:
                no(w.info, "too close to an edge or another hole")
                continue
            w.R, w.shrunk = limit, True
            w.rows = max(1, min(w.rows, round(math.log(w.R / w.r) / (2 * math.pi / w.n))))
            w.out_mm = 2 * math.pi * w.R * mm / w.n
            w.info["shrunk_width_mm"] = float(f"{(w.R - w.r) * mm:.4g}")
        w.info["washer"] = True
        keep.append(w)
    return keep, skipped, lines


def _on_hole(gmsh, w: Washer, curves: list[int]) -> bool:
    """Whether these curves lie on the washer's hole circle (the annulus's other loop; the face outside a ring has a
    loop of two too when the ring's hole is its only one)."""
    return all(bool(np.all(np.abs(np.linalg.norm(_samples(gmsh, c, 4) - w.centre, axis=1) - w.r) <= CIRCLE_TOL * w.r))
               for c in curves)


def _rings_whole(gmsh, t: int, ws: list[Washer], omap) -> bool:
    """Whether a fragment of face t by the washers' circles left each ring one circle closing round its hole."""
    new = [s for d, s in omap[0] if d == 2]
    for got in omap[1:len(ws) + 1]:
        curves = [c for d, c in got if d == 1]
        if len(curves) != 1:
            return False
        if not any([curves[0]] in [[abs(int(c)) for c in cl] for cl in gmsh.model.occ.getCurveLoops(s)[1]]
                   and len(gmsh.model.occ.getCurveLoops(s)[1]) == 2 for s in new):
            return False
    return True


def imprint(gmsh, washers: list[Washer], tag_of: dict[int, int], cuts: set[int] | None = None,
            preset: dict[int, int] | None = None) -> list[Washer]:
    """SHMESH-39/45f: each face's washers drawn on it in ONE cut - a round hole's ring (a circle about its centre,
    starting where the hole's circle starts), a square hole's four strips - so no washer holds a tag the cut has
    replaced (two cuts of one face, one for its rings and one for its square rings, left the second with a stale face:
    "Unknown OpenCASCADE entity of dimension 2"). `tag_of` follows; edges the face shares keep their tags, so every
    junction stays conformal. Tried on a copy first: a face where a ring would come out cut keeps no washer of that
    kind (rings, then square rings, tried alone). SHMESH-45g: a face that fails in any way after that keeps the washers
    drawn so far and loses only its own (logged, and the record says why). A square ring's counts go into `preset`,
    its lines into `cuts`. Returns the washers drawn."""
    by_face: dict[int, list[Washer]] = {}
    for w in washers:
        by_face.setdefault(w.face, []).append(w)
    kept: list[Washer] = []
    occ = gmsh.model.occ
    if preset is None:
        preset = {}

    def tools(ws: list[Washer]) -> list[tuple[int, int]]:
        out = [(1, occ.addCircle(*[float(x) for x in w.centre], float(w.R), zAxis=[float(x) for x in w.normal],
                                 xAxis=[float(x) for x in w.xaxis])) for w in ws if not w.corners]
        for w in ws:
            if w.corners:
                Q, ext = _frame_points(w)
                qp = [occ.addPoint(*[float(x) for x in q]) for q in Q]
                for i in range(4):
                    out.append((1, occ.addLine(qp[i], qp[(i + 1) % 4])))
                for p0, p1 in ext:
                    out.append((1, occ.addLine(occ.addPoint(*[float(x) for x in p0]),
                                               occ.addPoint(*[float(x) for x in p1]))))
        return out

    def trial(t: int, ws: list[Washer]) -> bool:
        rounds = [w for w in ws if not w.corners]
        copy = occ.copy([(2, t)])
        _o, cmap = occ.fragment(copy, tools(ws))
        occ.synchronize()
        cells = [x for d, x in cmap[0] if d == 2]
        ok = len(cells) == 1 + len(rounds) + 4 * (len(ws) - len(rounds)) and (
            not rounds or _rings_whole(gmsh, t, rounds, cmap))
        occ.remove([(2, x) for x in cells], recursive=True)
        occ.synchronize()
        _remove_loose(gmsh, {c for got in cmap[1:] for d, c in got if d == 1})
        return ok

    for t, ws in sorted(by_face.items()):
        if t not in tag_of:
            for w in ws:
                w.info.update(washer=False, why="its face was replaced before its ring was drawn")
            continue
        try:
            use = ws
            if not trial(t, use):
                rounds = [w for w in ws if not w.corners]
                squares = [w for w in ws if w.corners]
                use = rounds if rounds and trial(t, rounds) else (squares if squares and trial(t, squares) else [])
            for w in ws:
                if w not in use:
                    w.info.update(washer=False, why="its ring would cut another edge of its face" if not w.corners
                                  else "its square ring would cut another edge of its face")
            if not use:
                continue
            _out, omap = occ.fragment([(2, t)], tools(use))
            occ.synchronize()
            k = tag_of.pop(t)
            new = [x for d, x in omap[0] if d == 2]
            for x in new:
                tag_of[x] = k
            _remove_loose(gmsh, {c for got in omap[1 + sum(1 for w in use if not w.corners):]
                                 for d, c in got if d == 1})
            curves = sorted({abs(int(c)) for x in new for _d, c in gmsh.model.getBoundary([(2, x)], oriented=False)})
            for w, got in zip([w for w in use if not w.corners], omap[1:], strict=False):
                w.ring = [c for d, c in got if d == 1][0]
                for x in new:
                    _l, loops = occ.getCurveLoops(x)
                    loops = [[abs(int(c)) for c in cl] for cl in loops]
                    if len(loops) == 2 and [w.ring] in loops:
                        other = [cl for cl in loops if cl != [w.ring]][0]
                        if _on_hole(gmsh, w, other):
                            w.annulus = x
                            w.hole_after = other
                if w.annulus:
                    kept.append(w)
                else:
                    w.info.update(washer=False, why="its ring did not close round its hole")
            for w in use:
                if w.corners:
                    _square_presets(gmsh, w, curves, preset, cuts)
                    w.info["kind"] = "square ring in a free face"
                    kept.append(w)
        except Exception as exc:  # noqa: BLE001 - SHMESH-45g: one face's washers never cost the others
            import logging

            logging.getLogger(__name__).info("shell mesh: the washers of one face were not drawn (%s)", exc)
            for w in ws:
                if w not in kept:
                    w.info.update(washer=False, why=f"its face's washers failed ({type(exc).__name__}: {exc})"[:300])
    return kept


def set_counts(gmsh, washers: list[Washer]) -> set[int]:
    """SHMESH-39: the hole's circle and the ring's carry the washer's count, evenly spaced; returns those curves."""
    fixed: set[int] = set()
    for w in washers:
        lengths = [float(gmsh.model.occ.getMass(1, c)) for c in w.hole_after]
        counts = _spread(w.n, lengths)
        if counts is None:
            raise ValueError(f"a washer's hole has {len(lengths)} curves for {w.n} elements round it")
        for c, m in zip(w.hole_after, counts, strict=True):
            gmsh.model.mesh.setTransfiniteCurve(c, m + 1)
            fixed.add(c)
        gmsh.model.mesh.setTransfiniteCurve(w.ring, w.n + 1)
        fixed.add(w.ring)
    return fixed


def _loop_nodes(gmsh, curves: list[int], index: np.ndarray) -> np.ndarray:
    """The mesh nodes (0-based, the adapter's index) on these curves, each once."""
    out: list[int] = []
    for c in curves:
        tags, _x, _p = gmsh.model.mesh.getNodes(1, c, includeBoundary=True)
        out += index[np.asarray(tags, dtype=np.int64)].tolist()
    return np.array(sorted(set(out)), dtype=np.int64)


#: SHMESH-45d: the ring's nodes slide to their hole nodes' angles when they stand at most this share of an element off
#: them (more: the ring is refused and gmsh's mesh kept, SHMESH-45)
RING_FOLLOW = 0.75


def ring_mesh(gmsh, w: Washer, index: np.ndarray, xyz: np.ndarray, mm: float) -> tuple[np.ndarray, np.ndarray]:
    """SHMESH-39: the washer's own structured mesh, in place of whatever gmsh put between hole and ring: each node of the
    hole's circle joined to the ring's node at its angle, the rows between them on circles whose radii grow evenly
    (in ratio), so every quad is near square. The ring's own nodes are slid along the ring to their hole nodes' angles
    (in `xyz`, in place) when the hole's are not evenly spaced (SHMESH-45d). Returns (the new nodes, mm; the quads,
    node indices - new nodes numbered from len(xyz))."""
    inner = _loop_nodes(gmsh, w.hole_after, index)
    outer = _loop_nodes(gmsh, [w.ring], index)
    if len(inner) != w.n or len(outer) != w.n:
        raise ValueError(f"a washer's circles carry {len(inner)} and {len(outer)} nodes, not {w.n}")
    c = w.centre * mm
    e1 = w.xaxis
    e2 = np.cross(w.normal, e1)

    def angles(nodes: np.ndarray) -> np.ndarray:
        rel = xyz[nodes] - c
        return np.mod(np.arctan2(rel @ e2, rel @ e1), 2 * math.pi)

    a_in, a_out = angles(inner), angles(outer)
    inner, a_in = inner[np.argsort(a_in)], np.sort(a_in)
    outer, a_out = outer[np.argsort(a_out)], np.sort(a_out)
    step = 2 * math.pi / w.n
    diff = (a_out[None, :] - a_in[0] + math.pi) % (2 * math.pi) - math.pi
    shift = int(np.argmin(np.abs(diff[0])))
    outer, a_out = np.roll(outer, -shift), np.roll(a_out, -shift)
    gap = (a_out - a_in + math.pi) % (2 * math.pi) - math.pi
    if float(np.abs(gap).max()) > RING_FOLLOW * step:
        raise ValueError(f"a washer's hole and ring nodes do not face each other ({float(np.abs(gap).max()) / step:.2f} "
                         "of an element apart)")
    if float(np.abs(gap).max()) > 1e-9 * step:
        # SHMESH-45d: a hole whose nodes are not evenly spaced (its circle in uneven pieces, or pieces not exactly one
        # circle) - each ring node is slid along the ring to its hole node's angle, so every spoke is radial
        Rm = w.R * mm
        xyz[outer] = c + Rm * (np.cos(a_in)[:, None] * e1 + np.sin(a_in)[:, None] * e2)
        gap = np.zeros_like(gap)
    rings = [inner]
    new: list[np.ndarray] = []
    base = len(xyz)
    ratio = w.R / w.r
    for j in range(1, w.rows):
        f = j / w.rows
        rad = w.r * ratio ** f * mm
        ang = a_in + f * gap
        pts = c + rad * (np.cos(ang)[:, None] * e1 + np.sin(ang)[:, None] * e2)
        ids = np.arange(base, base + w.n)
        base += w.n
        new.append(pts)
        rings.append(ids)
    rings.append(outer)
    quads = []
    for a, b in zip(rings[:-1], rings[1:], strict=True):
        quads.append(np.column_stack([a, np.roll(a, -1), np.roll(b, -1), b]))
    return (np.vstack(new) if new else np.zeros((0, 3))), np.vstack(quads)


#: SHMESH-40: a face is mapped when its one loop has four CORNERS (where two curves meet between MAP_CORNER degrees;
#: where they run on within SMOOTH_DEG of straight it is no corner, so a side may be a chain of curves) and its
#: opposite sides are alike within MAP_ALIKE times (a grid stretched further leans or tapers)
MAP_CORNER = (40.0, 140.0)
SMOOTH_DEG = 20.0
MAP_ALIKE = 2.25
#: ... and no segment of a mapped side longer than MAP_LONGEST times the finest size its faces ask for
MAP_LONGEST = 1.4


def _ends(gmsh, c: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(start point, end point, direction leaving the start, direction leaving the end) of a curve."""
    lo, hi = gmsh.model.getParametrizationBounds(1, c)
    a, b = float(lo[0]), float(hi[0])
    pa = np.asarray(gmsh.model.getValue(1, c, [a]), dtype=float)
    pb = np.asarray(gmsh.model.getValue(1, c, [b]), dtype=float)
    da = np.asarray(gmsh.model.getDerivative(1, c, [a]), dtype=float)
    db = -np.asarray(gmsh.model.getDerivative(1, c, [b]), dtype=float)
    return pa, pb, da, db


def _four_sided(gmsh, t: int) -> tuple[list[list[int]], list[int]] | None:
    """(its four sides - each the curves along it, in loop order -, its four corner points) when the surface's one
    loop has exactly four corners and its other vertices are smooth; opposite sides alike within MAP_ALIKE; else
    None."""
    _l, loops = gmsh.model.occ.getCurveLoops(t)
    if len(loops) != 1:
        return None
    cl = [abs(int(c)) for c in loops[0]]
    if len(cl) < 4 or len(set(cl)) != len(cl):
        return None
    ends = {c: _ends(gmsh, c) for c in cl}
    pts = {c: [abs(int(p)) for _d, p in gmsh.model.getBoundary([(1, c)], oriented=True)] for c in cl}
    if any(len(v) != 2 or v[0] == v[1] for v in pts.values()):
        return None
    m = len(cl)
    corner_after: list[int | None] = []
    for j in range(m):
        a, b = cl[j], cl[(j + 1) % m]
        shared = set(pts[a]) & set(pts[b])
        if len(shared) != 1:
            return None
        p = shared.pop()
        # which end of each curve is at p: by where it lies (an edge may run against its own parameter)
        xp = np.asarray(gmsh.model.getValue(0, p, []), dtype=float)
        da = ends[a][2] if np.linalg.norm(ends[a][0] - xp) <= np.linalg.norm(ends[a][1] - xp) else ends[a][3]
        db = ends[b][2] if np.linalg.norm(ends[b][0] - xp) <= np.linalg.norm(ends[b][1] - xp) else ends[b][3]
        cos = float(da @ db) / max(float(np.linalg.norm(da) * np.linalg.norm(db)), 1e-300)
        ang = math.degrees(math.acos(max(-1.0, min(1.0, cos))))
        if MAP_CORNER[0] <= ang <= MAP_CORNER[1]:
            corner_after.append(p)
        elif ang >= 180.0 - SMOOTH_DEG:
            corner_after.append(None)
        else:
            return None
    at = [j for j in range(m) if corner_after[j] is not None]
    if len(at) != 4:
        return None
    sides: list[list[int]] = []
    corners: list[int] = []
    for i in range(4):
        start, stop = at[i] + 1, at[(i + 1) % 4] + 1
        idx = [(start + q) % m for q in range((stop - start) % m or m)]
        sides.append([cl[q] for q in idx])
        corners.append(int(corner_after[at[i]]))  # type: ignore[arg-type]
    length = [sum(float(gmsh.model.occ.getMass(1, c)) for c in sd) for sd in sides]
    for i in (0, 1):
        x, y = length[i], length[i + 2]
        if max(x, y) > MAP_ALIKE * max(min(x, y), 1e-300):
            return None
    return sides, corners


def _spread(total: int, lengths: list[float]) -> list[int] | None:
    """`total` segments over curves of these lengths, by length, each at least one; None when they do not fit."""
    if total < len(lengths):
        return None
    whole = sum(lengths)
    raw = [total * x / whole for x in lengths]
    out = [max(1, int(math.floor(v))) for v in raw]
    order = sorted(range(len(raw)), key=lambda i: (-(raw[i] - math.floor(raw[i])), i))
    k = 0
    while sum(out) < total:
        out[order[k % len(order)]] += 1
        k += 1
    while sum(out) > total:
        j = max((i for i in range(len(out)) if out[i] > 1), key=lambda i: (out[i] - raw[i], -i), default=None)
        if j is None:
            return None
        out[j] -= 1
    return out


def map_faces(gmsh, tag_of: dict[int, int], size_mm: dict[int, float], mm: float, *, skip: set[int],
              preset: dict[int, int], low_mm: float | None = None, fine_at: dict[int, float] | None = None,
              growth: float = 1.3, arc_mm=None, high_mm: float | None = None
              ) -> tuple[dict[int, list[int]], dict[int, int]]:
    """SHMESH-40: every four-cornered face (_four_sided) not in `skip` is meshed as a mapped grid. Opposite sides that
    are one curve each take one count, and so does every curve they share with other mapped faces (a pipe's pieces,
    a run of cells: one class of curves, one count). A class's count is its curves' mean length over the finest size
    of the faces it bounds (for an arc also `arc_mm(curve, face)`: the size the plan puts round that arc, so a pipe
    keeps its round), never so many that a segment comes out under `low_mm` (the user's shortest edge), made even when
    the class also bounds a free face (Blossom's condition there); a class holding a curve already counted (`preset`:
    a mapped strip's, a washer's) takes that count. A side that is a chain of curves takes its opposite side's count,
    spread over its curves by length. A face whose counts cannot agree (two presets in one class, a chain already
    counted otherwise) is left free, and the rest counted again. A face with a smaller piece of the CAD at a corner
    (`fine_at`: the size graded to there, mm, more than one growth step finer) stays free (a mapped grid cannot
    grade). Returns ({mapped face: its corners}, {curve: count})."""
    cand: dict[int, tuple[list[list[int]], list[int]]] = {}
    for t in sorted(tag_of):
        if t in skip:
            continue
        got = _four_sided(gmsh, t)
        if got is None:
            continue
        if fine_at and min((fine_at.get(p, math.inf) for p in got[1]), default=math.inf) * growth < size_mm[t]:
            continue
        cand[t] = got
    # nor next to a free face more than two growth steps finer (a grid cannot grade down to it; a mapped
    # neighbour shares its counts instead)
    for t in sorted(cand):
        near = {int(s) for sd in cand[t][0] for c in sd for s in gmsh.model.getAdjacencies(1, c)[0]
                if int(s) != t and int(s) in size_mm and int(s) not in cand}
        if any(size_mm[s] * growth ** 2 < size_mm[t] for s in near):
            cand.pop(t)
    length = {c: float(gmsh.model.occ.getMass(1, c)) for t in cand for sd in cand[t][0] for c in sd}
    counts: dict[int, int] = {}
    while True:
        parent: dict[int, int] = {}

        def root(c: int, parent: dict[int, int] = parent) -> int:
            parent.setdefault(c, c)
            while parent[c] != c:
                parent[c] = parent[parent[c]]
                c = parent[c]
            return c

        single: set[int] = set()
        for _t, (sides, _p) in sorted(cand.items()):
            for i in (0, 1):
                if len(sides[i]) == 1 and len(sides[i + 2]) == 1:
                    a, b = root(sides[i][0]), root(sides[i + 2][0])
                    single |= {sides[i][0], sides[i + 2][0]}
                    if a != b:
                        parent[max(a, b)] = min(a, b)
        members: dict[int, list[int]] = {}
        for c in sorted(single):
            members.setdefault(root(c), []).append(c)
        faces_of: dict[int, set[int]] = {}
        for t, (sides, _p) in cand.items():
            for sd in sides:
                for c in sd:
                    if c in single:
                        faces_of.setdefault(root(c), set()).add(t)
        bad: set[int] = set()
        counts = {}
        for r, cs in sorted(members.items()):
            pre = {preset[c] for c in cs if c in preset}
            if len(pre) > 1:
                bad |= faces_of[r]
                continue
            if pre:
                n = pre.pop()
            else:
                h = min(size_mm[t] for t in faces_of[r]) / mm
                if arc_mm is not None:
                    for c in cs:
                        for t in sorted(faces_of[r]):
                            if any(c in sd for sd in cand[t][0]):
                                got = arc_mm(c, t)
                                if got is not None:
                                    h = min(h, got / mm)
                n = max(1, round(float(np.mean([length[c] for c in cs])) / h))
                # the class's longest curve no coarser than MAP_LONGEST of the size (and under the user's longest edge)
                longest = max(length[c] for c in cs)
                n = max(n, math.ceil(longest / (MAP_LONGEST * h) - 1e-9))
                if high_mm is not None:
                    n = max(n, math.ceil(longest * mm / (0.98 * high_mm) - 1e-9))
                if low_mm is not None:
                    n = max(1, min(n, math.floor(min(length[c] for c in cs) * mm / low_mm + 1e-9)))
                free_beside = any(s not in cand for c in cs for s in gmsh.model.getAdjacencies(1, c)[0] if s in tag_of)
                if n > 1 and n % 2 and free_beside and (low_mm is None or min(length[c] for c in cs) * mm / (n + 1) >= low_mm):
                    n += 1
            for c in cs:
                counts[c] = n
        # chains: a side of several curves takes its opposite side's count, spread by length
        for t, (sides, _p) in sorted(cand.items()):
            if t in bad:
                continue
            for i in (0, 1):
                sa, sb = sides[i], sides[i + 2]
                if len(sa) == 1 and len(sb) == 1:
                    continue
                known = lambda sd, counts=counts: all(c in counts or c in preset for c in sd)  # noqa: E731
                total = lambda sd, counts=counts: sum(counts.get(c, preset.get(c, 0)) for c in sd)  # noqa: E731
                if known(sa) and known(sb):
                    if total(sa) != total(sb):
                        bad.add(t)
                    continue
                master, chain = (sa, sb) if known(sa) else (sb, sa) if known(sb) else (None, None)
                if master is None or chain is None:
                    bad.add(t)
                    continue
                fixed = [c for c in chain if c in counts or c in preset]
                left = [c for c in chain if c not in fixed]
                spread = _spread(total(master) - total(fixed), [length[c] for c in left]) if left else None
                if spread is None:
                    bad.add(t)
                    continue
                for c, m in zip(left, spread, strict=True):
                    counts[c] = m
        if not bad:
            break
        for t in bad:
            cand.pop(t)
    for c in sorted(set(counts) | {c for t in cand for sd in cand[t][0] for c in sd if c in preset}):
        counts.setdefault(c, preset.get(c, 0))
    for c, n in sorted(counts.items()):
        gmsh.model.mesh.setTransfiniteCurve(c, n + 1)
    out: dict[int, list[int]] = {}
    for t, (_s, corners) in sorted(cand.items()):
        gmsh.model.mesh.setTransfiniteSurface(t, cornerTags=corners)
        gmsh.model.mesh.setRecombine(2, t)
        out[t] = corners
    return out, counts


#: SHMESH-41: a flat face holding washers is cut into a grid of cells (a box round each washer, its sides drawn on to
#: the face's edges), so the field between the washers is mapped in rows along the face's edges. A washer whose ring
#: can meet the grid's size is an O-GRID: its ring and box in four mapped quarters (cut from the box's corners to the
#: hole), a box side carrying a quarter of the washer's count, each element BOX_SEG face sizes long, the box at least
#: BOX_GAP face sizes clear of the ring, the ring's outer elements no shorter than the face size over BOX_GROW. Any
#: other washer keeps its own ring (ST.ring_mesh) in a FREE BOX BOX_FREE face sizes out from the ring, paved from the
#: ring to the box (the transition stays inside the box). Lines closer than SNAP face sizes are one line (a box side
#: that near an edge runs to that edge); no cell edge under CELL_MIN face sizes.
BLOCKS = True
BOX_STEP = 8
BOX_SEG = (0.8, 1.4)
BOX_GAP = 0.4
BOX_GROW = 2.2
BOX_FREE = 1.0
SNAP = 0.75
CELL_MIN = 0.4
#: ... straight edges within PARALLEL_DEG of the face's frame count as running along it
PARALLEL_DEG = 1.0


def _boxed(r_mm: float, hole_mm: float, h_mm: float, low_mm: float | None = None,
           high_mm: float | None = None) -> tuple[int, int, float, float] | None:
    """SHMESH-41: (elements round, rows, ring over hole radius, box half-side mm) of an O-grid washer in a gridded face:
    the fewest elements round (a multiple of BOX_STEP - each quarter even: a hole's circle starts where the CAD put
    its seam, often mid-quarter, and an even quarter keeps its nodes evenly spaced -, at least WASHER_MIN and the
    plan's own count) and then the
    fewest rows such that the box side (a quarter of them) comes out BOX_SEG face sizes per element (and under the
    user's longest edge `high_mm`), the box BOX_GAP face sizes clear of the ring, the ring between WASHER_RING times the
    hole, its rows no thinner than the user's shortest edge `low_mm`, and its outer elements no shorter than the face
    size over BOX_GROW; None when none does (the washer then takes a free box)."""
    own = math.ceil(2 * math.pi * r_mm / max(hole_mm, 1e-12) - 1e-9)
    n0 = max(WASHER_MIN, BOX_STEP * math.ceil(own / BOX_STEP))
    seg_hi = BOX_SEG[1] * h_mm if high_mm is None else min(BOX_SEG[1] * h_mm, 0.98 * high_mm)
    for n in range(n0, WASHER_MAX + 1, BOX_STEP):
        if low_mm is not None and n > n0 and 2 * math.pi * r_mm / n < low_mm:
            break
        q = math.exp(2 * math.pi / n)
        hi = n * seg_hi / 8
        for rows in range(1, WASHER_ROWS + 1):
            r_hi = min(WASHER_RING[1] * r_mm, hi - BOX_GAP * h_mm)
            r_lo = max(WASHER_RING[0] * r_mm, n * h_mm / (2 * math.pi * BOX_GROW),
                       r_mm + rows * (low_mm or 0.0))
            if r_lo > r_hi:
                continue
            R = min(max(q ** rows * r_mm, r_lo), r_hi)
            b = max(R + BOX_GAP * h_mm, n * BOX_SEG[0] * h_mm / 8, n * h_mm / 8)
            return n, rows, R / r_mm, min(b, hi)
    return None


def _frame(gmsh, t: int, outer: list[int]) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """(origin, u, v) of a flat face: u along its longest straight outer edge, v square to it in the face."""
    best, u = 0.0, None
    for c in outer:
        if gmsh.model.getType(1, c) != "Line":
            continue
        pa, pb, _da, _db = _ends(gmsh, c)
        d = pb - pa
        ln = float(np.linalg.norm(d))
        if ln > best:
            best, u = ln, d / ln
    if u is None:
        return None
    lo, hi = gmsh.model.getParametrizationBounds(2, t)
    n = np.asarray(gmsh.model.getNormal(t, [0.5 * (lo[0] + hi[0]), 0.5 * (lo[1] + hi[1])]), dtype=float)[:3]
    n = n / max(float(np.linalg.norm(n)), 1e-300)
    u = u - float(u @ n) * n
    u /= max(float(np.linalg.norm(u)), 1e-300)
    v = np.cross(n, u)
    o = _samples(gmsh, outer[0], 1)[0]
    return o, u, v


def _cluster(values: list[tuple[float, str]], tol: float) -> list[tuple[float, bool, list[int]]]:
    """Values along one axis ((value, kind): kind "edge" for a straight edge of the face there, else a box side's
    index) joined where neighbours are closer than tol: [(the value - an edge's where one stands -, whether an edge
    stands there, the box sides on it)]."""
    out: list[tuple[float, bool, list[int]]] = []
    group: list[tuple[float, str]] = []
    for val, kind in sorted(values) + [(math.inf, "")]:
        if group and val - group[-1][0] > tol:
            edges = [x for x, k in group if k == "edge"]
            at = float(np.mean(edges)) if edges else float(np.mean([x for x, _k in group]))
            out.append((at, bool(edges), [int(k) for _x, k in group if k != "edge"]))
            group = []
        if math.isfinite(val):
            group.append((val, kind))
    return out


def _remove_loose(gmsh, curves: set[int]) -> None:
    """Take out the curves among these that bound no surface (pieces of a cutting line outside the face)."""
    have = {c for _d, c in gmsh.model.getEntities(1)}
    loose = [(1, c) for c in sorted(curves & have) if not len(gmsh.model.getAdjacencies(1, c)[0])]
    if loose:
        gmsh.model.occ.remove(loose, recursive=True)
        gmsh.model.occ.synchronize()


def block_faces(gmsh, washers: list[Washer], tag_of: dict[int, int], size_mm: dict[int, float], hole_size, mm: float,
                cuts: set[int] | None = None, low_mm: float | None = None, high_mm: float | None = None,
                cells_out: set[int] | None = None
                ) -> tuple[list[Washer], list[Washer], dict[int, int], set[int], dict[str, int]]:
    """SHMESH-41: each flat face whose inner loops are all washer holes is cut into a grid: round each hole a box
    along the face's own frame (its longest straight edge) - an O-grid's (_boxed) or a free box - every box side drawn
    as a line right across the face; lines nearer each other than SNAP face sizes are one, and a box side that near a
    straight edge of the face is that edge. The cells come out four-cornered where the face's edges run along the
    frame, so map_faces maps them. A face is left to the free washers when a line would cross a box, a ring would not
    fit its box, an O-grid box side's elements would come out outside BOX_SEG, or a cell edge would come out under
    CELL_MIN face sizes (tried on a copy of the face first). Returns (the O-grid washers, the free-box washers - each
    with its ring and annulus drawn -, {curve: count} the grid must keep: the O-grid rings' quarters and rows, the
    O-grid holes' curves (for the arc sizes to leave alone), {why a face was not cut: count}). `cuts` gathers the box
    lines drawn here (no edge of the CAD: the nodes on them may move in the face's plane)."""
    by_face: dict[int, list[Washer]] = {}
    for w in washers:
        by_face.setdefault(w.face, []).append(w)
    ogrid: list[Washer] = []
    boxed: list[Washer] = []
    preset: dict[int, int] = {}
    holes: set[int] = set()
    why: dict[str, int] = {}

    def no(reason: str) -> None:
        why[reason] = why.get(reason, 0) + 1

    for t, ws in sorted(by_face.items()):
        # SHMESH-45g: a face whose grid fails in any way loses only its own grid (its washers go on to the free
        # rings, or to none where the face was already cut); the other faces keep theirs
        try:
            h_mm = size_mm[t]
            h = h_mm / mm
            _l, loops = gmsh.model.occ.getCurveLoops(t)
            loops = [[abs(int(c)) for c in cl] for cl in loops]
            hole_loops = [sorted(w.hole) for w in ws]
            rest = [cl for cl in loops if sorted(cl) not in hole_loops]
            if len(rest) != 1:
                no("a hole without a washer in the face")
                continue
            outer = rest[0]
            fr = _frame(gmsh, t, outer)
            if fr is None:
                no("no straight edge to line up with")
                continue
            o, u, v = fr

            def uv(p: np.ndarray, o=o, u=u, v=v) -> tuple[float, float]:
                return float((p - o) @ u), float((p - o) @ v)

            plans = [_boxed(w.r * mm, hole_size(w.r * mm, w.owner), h_mm, low_mm, high_mm) for w in ws]
            vals: dict[str, list[tuple[float, str]]] = {"u": [], "v": []}
            lo = np.array([math.inf, math.inf])
            hi = -lo
            cos_par = math.cos(math.radians(PARALLEL_DEG))
            for c in outer:
                pts = _samples(gmsh, c, 16)
                q = np.array([uv(p) for p in pts])
                lo, hi = np.minimum(lo, q.min(axis=0)), np.maximum(hi, q.max(axis=0))
                if gmsh.model.getType(1, c) == "Line":
                    d = pts[-1] - pts[0]
                    d = d / max(float(np.linalg.norm(d)), 1e-300)
                    if abs(float(d @ u)) >= cos_par:
                        vals["v"].append((float(q[:, 1].mean()), "edge"))
                    if abs(float(d @ v)) >= cos_par:
                        vals["u"].append((float(q[:, 0].mean()), "edge"))
            boxes = []
            for j, (w, p) in enumerate(zip(ws, plans, strict=True)):
                cu, cv = uv(w.centre)
                b = p[3] / mm if p is not None else w.R + BOX_FREE * h
                boxes.append([cu - b, cu + b, cv - b, cv + b])
                vals["u"] += [(cu - b, str(4 * j)), (cu + b, str(4 * j + 1))]
                vals["v"] += [(cv - b, str(4 * j + 2)), (cv + b, str(4 * j + 3))]
            lines: dict[str, list[float]] = {"u": [], "v": []}
            for ax in ("u", "v"):
                for at, edge, sides in _cluster(vals[ax], SNAP * h):
                    for sd in sides:
                        boxes[sd // 4][sd % 4] = at
                    if not edge and sides:
                        lines[ax].append(at)
            reason = ""
            eps = 1e-6 * h
            for j, (w, p) in enumerate(zip(ws, plans, strict=True)):
                cu, cv = uv(w.centre)
                bx = boxes[j]
                R = p[2] * w.r if p is not None else w.R
                room = min(cu - bx[0], bx[1] - cu, cv - bx[2], bx[3] - cv)
                if room < R + (0.3 if p is not None else 0.5) * h:
                    reason = "a ring too near an edge or another ring for its box"
                elif p is not None and not all(0.6 * h <= x / (p[0] / 4) <= 1.5 * h
                                               for x in (bx[1] - bx[0], bx[3] - bx[2])):
                    reason = "a box drawn on to an edge comes out too long or short for its ring"
                elif any(bx[0] + eps < x < bx[1] - eps for x in lines["u"]) or any(
                        bx[2] + eps < y < bx[3] - eps for y in lines["v"]):
                    reason = "the holes do not line up (a line would cross a box)"
                if reason:
                    break
            if reason:
                no(reason)
                continue
            pad = 0.05 * float((hi - lo).max())
            n_face = np.cross(u, v)
            corners_of = [[o + bx[0] * u + bx[2] * v, o + bx[1] * u + bx[2] * v, o + bx[1] * u + bx[3] * v,
                           o + bx[0] * u + bx[3] * v] for bx in boxes]
            # each O-grid's quarters start at its first box corner's direction
            starts = {}
            for w, p, cs in zip(ws, plans, corners_of, strict=True):
                if p is not None:
                    d0 = cs[0] - w.centre
                    starts[id(w)] = d0 / max(float(np.linalg.norm(d0)), 1e-300)

            def tools(o=o, u=u, v=v, lo=lo, hi=hi, pad=pad, lines=lines, ws=ws, plans=plans, corners_of=corners_of,
                      starts=starts, n_face=n_face) -> list[tuple[int, int]]:
                out = []
                occ = gmsh.model.occ
                for x in lines["u"]:
                    a, b = o + x * u + (lo[1] - pad) * v, o + x * u + (hi[1] + pad) * v
                    out.append((1, occ.addLine(occ.addPoint(*a), occ.addPoint(*b))))
                for y in lines["v"]:
                    a, b = o + (lo[0] - pad) * u + y * v, o + (hi[0] + pad) * u + y * v
                    out.append((1, occ.addLine(occ.addPoint(*a), occ.addPoint(*b))))
                for w, p, cs in zip(ws, plans, corners_of, strict=True):
                    R = p[2] * w.r if p is not None else w.R
                    xa = starts[id(w)] if p is not None else w.xaxis
                    out.append((1, occ.addCircle(*[float(x) for x in w.centre], float(R),
                                                 zAxis=[float(x) for x in n_face], xAxis=[float(x) for x in xa])))
                    if p is not None:
                        for cp in cs:
                            out.append((1, occ.addLine(occ.addPoint(*cp), occ.addPoint(*[float(x) for x in w.centre]))))
                return out

            # tried on a copy of the face first: no edge of a cell under CELL_MIN face sizes
            shared = [c for cl in loops for c in cl if len(gmsh.model.getAdjacencies(1, c)[0]) > 1]
            copy = gmsh.model.occ.copy([(2, t)])
            _o, cmap = gmsh.model.occ.fragment(copy, tools())
            gmsh.model.occ.synchronize()
            cells = [s for d, s in cmap[0] if d == 2]
            cut_curves = {c for got in cmap[1:] for d, c in got if d == 1}
            small = any(min(float(gmsh.model.occ.getMass(1, abs(c))) for _d, c in
                            gmsh.model.getBoundary([(2, s)], oriented=False)) < CELL_MIN * h for s in cells)
            # and every ring came out as drawn: one circle round a free box's hole, four quarters round an O-grid's
            trial = sorted({abs(int(c)) for s in cells for _d, c in gmsh.model.getBoundary([(2, s)], oriented=False)})
            # and no line cuts an edge the face shares with another (the other face would keep it whole: a crack)
            if shared:
                from scipy.spatial import cKDTree

                for c in shared:
                    tree = cKDTree(_samples(gmsh, c, 256))
                    full = float(gmsh.model.occ.getMass(1, c))
                    for x in trial:
                        if tree.query(_samples(gmsh, x, 4))[0].max() <= 1e-3 * h + full / 256 and                             float(gmsh.model.occ.getMass(1, x)) < (1 - 1e-6) * full:
                            small = True
            for w, p in zip(ws, plans, strict=True):
                R = p[2] * w.r if p is not None else w.R
                on_ring = [c for c in trial if np.all(np.abs(np.linalg.norm(_samples(gmsh, c, 4) - w.centre, axis=1) - R)
                                                      <= 1e-4 * R)]
                if len(on_ring) != (4 if p is not None else 1):
                    small = True
            gmsh.model.occ.remove([(2, s) for s in cells], recursive=True)
            gmsh.model.occ.synchronize()
            _remove_loose(gmsh, cut_curves)
            if small:
                no("a cell would come out too small, a ring or a shared edge cut")
                continue
            edge_pts = np.vstack([_samples(gmsh, c, 64) for cl in loops for c in cl])
            _o, omap = gmsh.model.occ.fragment([(2, t)], tools())
            gmsh.model.occ.synchronize()
            k = tag_of.pop(t)
            new = [s for d, s in omap[0] if d == 2]
            for s in new:
                tag_of[s] = k
            if cells_out is not None:
                cells_out.update(new)
            _remove_loose(gmsh, {c for got in omap[1:] for d, c in got if d == 1})
            curves = sorted({abs(int(c)) for s in new for _d, c in gmsh.model.getBoundary([(2, s)], oriented=False)})
            on_circle: set[int] = set()
            for w, p in zip(ws, plans, strict=True):
                R = p[2] * w.r if p is not None else w.R
                tol = 1e-4 * R
                ring_c: list[int] = []
                hole_c: list[int] = []
                for c in curves:
                    d = np.linalg.norm(_samples(gmsh, c, 4) - w.centre, axis=1)
                    if np.all(np.abs(d - w.r) <= CIRCLE_TOL * w.r):
                        hole_c.append(c)
                    elif np.all(np.abs(d - R) <= tol):
                        ring_c.append(c)
                    elif p is not None and d.min() >= w.r - tol and d.max() <= R + tol and abs(d[0] - d[-1]) > 0.5 * (R - w.r):
                        preset[c] = p[1]
                on_circle |= set(ring_c) | set(hole_c)
                if p is not None:
                    for c in ring_c:
                        preset[c] = p[0] // 4
                    holes.update(hole_c)
                    w.n, w.rows, w.R = p[0], p[1], R
                    w.out_mm = 2 * math.pi * R * mm / p[0]
                    ogrid.append(w)
                else:
                    if len(ring_c) != 1:
                        raise ValueError("a washer's ring did not come out as one circle")
                    w.ring, w.hole_after = ring_c[0], hole_c
                    for s in new:
                        _l3, lp = gmsh.model.occ.getCurveLoops(s)
                        lp = [sorted(abs(int(c)) for c in cl) for cl in lp]
                        if len(lp) == 2 and [w.ring] in lp and _on_hole(gmsh, w, [c for cl in lp for c in cl if c != w.ring]):
                            w.annulus = s
                    if not w.annulus:
                        raise ValueError("a washer's ring did not close round its hole")
                    boxed.append(w)
            if cuts is not None:
                from scipy.spatial import cKDTree

                tree = cKDTree(edge_pts)
                for c in curves:
                    if c in on_circle:
                        continue
                    if tree.query(_samples(gmsh, c, 2)[1])[0] > 1e-3 * h:
                        cuts.add(c)
        except Exception as exc:  # noqa: BLE001
            ogrid[:] = [w for w in ogrid if w not in ws]
            boxed[:] = [w for w in boxed if w not in ws]
            for w in ws:
                w.ring, w.annulus, w.hole_after = 0, 0, []
            no(f"its grid failed ({type(exc).__name__}: {exc})"[:200])
    return ogrid, boxed, preset, holes, why


#: SHMESH-45b: the round free holes of flat faces drawn again as one circle (a CAD circle split into 2 to 5 edges,
#: unevenly, left the washer's hole nodes out of step with its ring's: "a washer's hole and ring nodes do not face each
#: other"), starting on the face frame's diagonal where the face has a straight edge
RESEAT = True

#: SHMESH-45f: four-sided holes take a square ring (off in the guard's round-rings-only step)
SQUARES = True


#: SHMESH-45d: pieces of a hole are one circle when every point of them lies within this share of the radius of one
#: fitted circle (the washer's own round test, _circle_of, is 1e-3 too)
REDRAW_TOL = 1e-3


def _fit_circle_occ(edges: list, normal: np.ndarray) -> tuple[np.ndarray, float, np.ndarray] | None:
    """(centre, radius, axis) of the circle the edges lie on within REDRAW_TOL of its radius, going once round; None
    when they do not (a slot, a polygon, an ellipse)."""
    from OCP.BRepAdaptor import BRepAdaptor_Curve

    pts = []
    for e in edges:
        c = BRepAdaptor_Curve(e)
        for x in np.linspace(c.FirstParameter(), c.LastParameter(), 33):
            p = c.Value(float(x))
            pts.append([p.X(), p.Y(), p.Z()])
    P = np.asarray(pts)
    c0 = P.mean(axis=0)
    _u, _s, vt = np.linalg.svd(P - c0, full_matrices=False)
    n = vt[2] if float(vt[2] @ normal) >= 0 else -vt[2]
    xy = np.column_stack([(P - c0) @ vt[0], (P - c0) @ vt[1]])
    sol, *_r = np.linalg.lstsq(np.column_stack([2 * xy, np.ones(len(xy))]), (xy ** 2).sum(axis=1), rcond=None)
    centre = c0 + sol[0] * vt[0] + sol[1] * vt[1]
    rad = np.linalg.norm(P - centre, axis=1)
    r = float(rad.mean())
    if r <= 0 or float(np.abs(rad - r).max()) > REDRAW_TOL * r or float(np.abs((P - centre) @ n).max()) > REDRAW_TOL * r:
        return None
    ang = np.sort(np.arctan2(xy[:, 1] - sol[1], xy[:, 0] - sol[0]))
    if float(np.diff(np.concatenate([ang, ang[:1] + 2 * math.pi])).max()) > 0.5 * math.pi:
        return None
    return centre, r, n


def _wire_turn(edges: list, centre: np.ndarray, axis: np.ndarray) -> float:
    """The sense a hole's wire goes round its centre about `axis` (+ anticlockwise), as its edges run in the wire."""
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.TopAbs import TopAbs_REVERSED

    total = 0.0
    for e in edges:
        c = BRepAdaptor_Curve(e)
        ts = np.linspace(c.FirstParameter(), c.LastParameter(), 9)
        P = np.array([[c.Value(float(t)).X(), c.Value(float(t)).Y(), c.Value(float(t)).Z()] for t in ts]) - centre
        turn = sum(float(np.cross(a, b) @ axis) for a, b in zip(P[:-1], P[1:], strict=True))
        total += -turn if e.Orientation() == TopAbs_REVERSED else turn
    return total


def reseat_holes(brep, out) -> tuple[int, list]:
    """SHMESH-45b: every round hole of a flat face that no other face shares is drawn again as ONE circle starting on the
    diagonal of the face's frame (its longest straight outer edge, as block_faces reads it), the same circle exactly. A
    CAD hole's circle starts at its seam, often in the middle of an O-grid quarter, and the quarter then cannot be
    counted evenly; drawn again, the seam sits where the O-grid cuts anyway. Only the hole's own wire changes (edges
    shared with other faces are untouched, so every junction stays as it is); a face whose area would change is left
    as it was. Writes `out` and returns (how many holes were drawn again - 0: `out` is the input unchanged -, [(each
    one's centre, how many CAD edges it had)])."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeWire
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepTools import BRepTools, BRepTools_ReShape
    from OCP.GCPnts import GCPnts_AbscissaPoint
    from OCP.GeomAbs import GeomAbs_Circle, GeomAbs_Line, GeomAbs_Plane
    from OCP.gp import gp_Ax2, gp_Circ, gp_Dir, gp_Pnt
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_REVERSED, TopAbs_WIRE
    from OCP.TopExp import TopExp, TopExp_Explorer
    from OCP.TopoDS import TopoDS, TopoDS_Shape
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    from meshpipeline.cad import midsurface as ms

    shape = TopoDS_Shape()
    if not BRepTools.Read_s(shape, str(brep), BRep_Builder()):
        raise ValueError("the mid-surface could not be read")
    emap = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, emap)

    def area(f) -> float:
        g = GProp_GProps()
        BRepGProp.SurfaceProperties_s(f, g)
        return float(g.Mass())

    def edges(w) -> list:
        out_, ex = [], TopExp_Explorer(w, TopAbs_EDGE)
        while ex.More():
            out_.append(TopoDS.Edge_s(ex.Current()))
            ex.Next()
        return out_

    done = 0
    redrawn: list = []
    reshape = BRepTools_ReShape()
    for f in ms._faces(shape):
        sa = BRepAdaptor_Surface(f)
        if sa.GetType() != GeomAbs_Plane:
            continue
        outer = BRepTools.OuterWire_s(f)
        best, u = 0.0, None
        for e in edges(outer):
            c = BRepAdaptor_Curve(e)
            if c.GetType() == GeomAbs_Line:
                ln = float(GCPnts_AbscissaPoint.Length_s(c))
                if ln > best:
                    d = c.Line().Direction()
                    best, u = ln, np.array([d.X(), d.Y(), d.Z()])
        ax = sa.Plane().Axis().Direction()
        nrm = np.array([ax.X(), ax.Y(), ax.Z()])
        diag = None
        if u is not None:
            u = u - float(u @ nrm) * nrm
            u /= max(float(np.linalg.norm(u)), 1e-300)
            diag = (u + np.cross(nrm, u)) / math.sqrt(2.0)
        a0 = area(f)
        ex = TopExp_Explorer(f, TopAbs_WIRE)
        swaps = []
        radii: list[float] = []
        while ex.More():
            w = TopoDS.Wire_s(ex.Current())
            ex.Next()
            if w.IsSame(outer):
                continue
            es = edges(w)
            if not es or any(emap.FindFromKey(e).Size() != 1 for e in es):
                continue
            # SHMESH-45d: one circle within REDRAW_TOL of its radius - pieces that are B-splines or arcs of slightly
            # different centre and radius (a mid-surface's fitted hole) are one circle too
            fit = _fit_circle_occ(es, nrm)
            if fit is None:
                continue
            loc, radius, caxis_fit = fit
            circs = [BRepAdaptor_Curve(e) for e in es]
            exact = all(cv.GetType() == GeomAbs_Circle for cv in circs)
            p0 = circs[0].Value(circs[0].FirstParameter())
            rel = np.array([p0.X() - loc[0], p0.Y() - loc[1], p0.Z() - loc[2]])
            rel /= max(float(np.linalg.norm(rel)), 1e-300)
            if len(es) == 1 and exact and (diag is None or float(rel @ diag) > 1 - 1e-9):
                continue            # already one circle (starting on the diagonal when the face has one)
            start = diag if diag is not None else rel
            caxis = caxis_fit
            if exact:
                cax = circs[0].Circle().Axis().Direction()
                caxis = np.array([cax.X(), cax.Y(), cax.Z()])
            xd = start - float(start @ caxis) * caxis
            circ = gp_Circ(gp_Ax2(gp_Pnt(*[float(x) for x in loc]), gp_Dir(*caxis.tolist()), gp_Dir(*xd.tolist())),
                           float(radius))
            nw = BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(circ).Edge()).Wire()
            if exact:
                if es[0].Orientation() == TopAbs_REVERSED:
                    nw.Reverse()
            elif _wire_turn(es, loc, caxis) < 0:
                nw.Reverse()
            swaps.append((w, nw))
            radii.append(float(radius))
            redrawn.append(([float(x) for x in loc], len(es)))
        if not swaps:
            continue
        trial = BRepTools_ReShape()
        for w, nw in swaps:
            trial.Replace(w, nw)
        nf = TopoDS.Face_s(trial.Apply(f))
        # the face's area kept: to a millionth, or to what the holes' own fitting tolerance can move it
        give = sum(2 * math.pi * float(r_) * REDRAW_TOL * float(r_) for r_ in radii)
        if abs(area(nf) - a0) > 1e-6 * max(a0, 1e-300) + give:
            continue
        for w, nw in swaps:
            reshape.Replace(w, nw)
        done += len(swaps)
    if not done:
        BRepTools.Write_s(shape, str(out))
        return 0, []
    result = reshape.Apply(shape)
    if len(ms._faces(result)) != len(ms._faces(shape)):
        BRepTools.Write_s(shape, str(out))
        return 0, []
    BRepTools.Write_s(result, str(out))
    return done, redrawn


__all__ = ["WASHERS", "MAPPED", "Washer", "plan_washers", "imprint", "set_counts", "ring_mesh", "map_faces", "block_faces", "reseat_holes"]
