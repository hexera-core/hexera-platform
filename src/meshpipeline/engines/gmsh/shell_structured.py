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
    in_box: bool = False            # SHMESH-48h: a square ring drawn in a free box of a gridded face
    seam: list = field(default_factory=list)  # SHMESH-49e: where its O-grid cuts the hole (the redraw starts it there)
    ring: int = 0                   # after the imprint: the ring's circle
    annulus: int = 0                # ... the surface between hole and ring
    hole_after: list[int] = field(default_factory=list)
    info: dict = field(default_factory=dict)  # SHMESH-45c: the hole's line in the record (quality.structure.hole_list)


def _curve_loops(gmsh, t: int) -> tuple[list, list]:
    """SHMESH-45h: gmsh's curve loops of a surface without the empty ones it gives for some faces (an empty loop read
    as a hole raised in the hole record, or kept a face from its grid)."""
    tags, loops = gmsh.model.occ.getCurveLoops(t)
    keep = [i for i, cl in enumerate(loops) if len(cl)]
    return [tags[i] for i in keep], [loops[i] for i in keep]


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
        _l, loops = _curve_loops(gmsh, t)
        # (an empty loop - gmsh gives one for some faces - is no hole: the record's sampling of it raised and cost
        # every washer of the part, SHMESH-45h)
        loops = [[abs(int(c)) for c in cl] for cl in loops if len(cl)]
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


def _on_square(gmsh, w: Washer, curves: list[int]) -> bool:
    """SHMESH-48h: whether these curves (a loop) lie on a square washer's hole sides and go all round it."""
    if not curves or not w.corners:
        return False
    P = w.corners
    tol = 1e-4 * max(float(w.r), 1e-12)
    total = 0.0
    for c in curves:
        q = _samples(gmsh, c, 4)
        dist = np.full(len(q), np.inf)
        for i in range(4):
            a_, d_ = P[i], P[(i + 1) % 4] - P[i]
            tt = np.clip(((q - a_) @ d_) / max(float(d_ @ d_), 1e-300), 0.0, 1.0)
            dist = np.minimum(dist, np.linalg.norm(a_ + tt[:, None] * d_ - q, axis=1))
        if float(dist.max()) > tol:
            return False
        total += float(gmsh.model.occ.getMass(1, c))
    rim = sum(float(np.linalg.norm(P[(i + 1) % 4] - P[i])) for i in range(4))
    return abs(total - rim) <= 1e-3 * rim


def _rings_whole(gmsh, t: int, ws: list[Washer], omap) -> bool:
    """Whether a fragment of face t by the washers' circles left each ring one circle closing round its hole."""
    new = [s for d, s in omap[0] if d == 2]
    for got in omap[1:len(ws) + 1]:
        curves = [c for d, c in got if d == 1]
        if len(curves) != 1:
            return False
        if not any([curves[0]] in [[abs(int(c)) for c in cl] for cl in _curve_loops(gmsh, s)[1]]
                   and len(_curve_loops(gmsh, s)[1]) == 2 for s in new):
            return False
    return True


def imprint(gmsh, washers: list[Washer], tag_of: dict[int, int], cuts: set[int] | None = None,
            preset: dict[int, int] | None = None, square_cells: set[int] | None = None) -> list[Washer]:
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
            if cuts is not None and any(w.in_box for w in use):
                # SHMESH-48h: a box cell's edges that no other face shares come back with new tags from the cut
                cuts.intersection_update(c for _d, c in gmsh.model.getEntities(1))
            curves = sorted({abs(int(c)) for x in new for _d, c in gmsh.model.getBoundary([(2, x)], oriented=False)})
            for w, got in zip([w for w in use if not w.corners], omap[1:], strict=False):
                w.ring = [c for d, c in got if d == 1][0]
                for x in new:
                    _l, loops = _curve_loops(gmsh, x)
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
            if square_cells is not None and any(w.corners for w in use):
                square_cells.update(new)
            for w in use:
                if w.corners:
                    _square_presets(gmsh, w, curves, preset, cuts)
                    w.info["kind"] = "square ring in a free box of a gridded face" if w.in_box \
                        else "square ring in a free face"
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
        w.info["followed"] = float(f"{float(np.abs(gap).max()) / step:.3g}")      # SHMESH-45k: drawn again next pass
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
    _l, loops = _curve_loops(gmsh, t)
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
    # SHMESH-47: a class whose curve stands opposite a chain of counted curves takes the chain's sum (a fillet cut at a
    # gridded web's lines carries the web's columns on to the flange): {a curve of the class: its count}
    forced: dict[int, int] = {}
    rounds = 0
    while True:
        rounds += 1
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
        newly = False
        for r, cs in sorted(members.items()):
            pre = {preset[c] for c in cs if c in preset} | {forced[c] for c in cs if c in forced}
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
                for one, many in ((sa, sb), (sb, sa)):
                    if (len(one) == 1 and len(many) > 1 and one[0] in single and one[0] not in preset
                            and known(many) and total(one) != total(many)):
                        # a single curve with a class of its own opposite a counted chain: the class takes the sum
                        if forced.get(one[0]) not in (None, total(many)):
                            bad.add(t)
                        else:
                            forced[one[0]] = total(many)
                            newly = True
                if newly or t in bad:
                    continue
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
        if newly and rounds < 8:
            continue            # counted again with the classes the chains set
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

#: SHMESH-47: a grid line ending on an edge the face shares cuts the neighbour there too (else the face is not gridded)
CUT_NEIGHBOURS = True
#: SHMESH-48f: every gridded face judged on its own elements once meshed (as POLAR_CHECK judges a round plate)
GRID_CHECK = True
#: SHMESH-48g: a grid line that would cross a box stops at it (else the face is not gridded)
STOP_AT_BOXES = True
BOX_STEP = 8
#: SHMESH-48: the ring an O-grid aims at, times the hole's radius (an engineer's washer: 1.5 to 2 times its diameter)
WASHER_AIM = 1.75
BOX_SEG = (0.8, 1.4)
BOX_GAP = 0.4
BOX_GROW = 2.2
BOX_FREE = 1.0
SNAP = 0.75
#: SHMESH-48i: near lines are made one only where one place keeps every ring on them inside its box
SNAP_LIMITS = True
CELL_MIN = 0.4
#: ... straight edges within PARALLEL_DEG of the face's frame count as running along it
PARALLEL_DEG = 1.0


def _boxed(r_mm: float, hole_mm: float, h_mm: float, low_mm: float | None = None,
           high_mm: float | None = None) -> tuple[int, int, float, float] | None:
    """SHMESH-41/48: (elements round, rows, ring over hole radius, box half-side mm) of an O-grid washer in a gridded
    face, every edge of it inside a band: the user's shortest and longest edge when they gave them (the count round the
    hole is then picked to fit the band, never fewer than 8), else our own (the plan's count round the hole at least,
    box elements BOX_SEG face sizes). Searched in a fixed order: the fewest elements round, then two rows before one,
    then the ring nearest WASHER_AIM times the hole (an engineer's washer: 1.5 to 2 times its diameter) that leaves
    the box clear of it, then the box side nearest one face size a segment. The box stands clear of the ring by its
    side-middle row (no shorter than the band's low end, nor BOX_GAP face sizes); the ring's outer elements are no
    shorter than the face size over BOX_GROW. The hole's circle starts on the box's diagonal (SHMESH-45b), so any count
    a multiple of WASHER_STEP splits evenly into quarters. None when nothing fits (the washer then takes a free box)."""
    user = low_mm is not None or high_mm is not None
    e_lo = float(low_mm or 0.0)
    e_hi = 0.98 * float(high_mm) if high_mm is not None else math.inf
    seg_lo, seg_hi = max(BOX_SEG[0] * h_mm, e_lo), min(BOX_SEG[1] * h_mm, e_hi)
    own = math.ceil(2 * math.pi * r_mm / max(hole_mm, 1e-12) - 1e-9)
    n0 = WASHER_MIN if user else max(WASHER_MIN, WASHER_STEP * math.ceil(own / WASHER_STEP))
    for n in range(n0, WASHER_MAX + 1, WASHER_STEP):
        s_hole = 2 * math.pi * r_mm / n
        if s_hole < e_lo or s_hole > e_hi:
            continue
        for rows in (2, 1):
            row_hi = min(e_hi, 1.2 * h_mm)
            gap = max(e_lo, BOX_GAP * h_mm)
            b_hi = n * seg_hi / 8
            r_lo = max(WASHER_RING[0] * r_mm, r_mm + rows * e_lo, n * h_mm / (2 * math.pi * BOX_GROW))
            r_hi = min(WASHER_RING[1] * r_mm, r_mm + rows * row_hi, n * e_hi / (2 * math.pi), b_hi - gap)
            if r_lo > r_hi:
                continue
            R = min(max(WASHER_AIM * r_mm, r_lo), r_hi)
            b_lo = max(R + gap, n * seg_lo / 8)
            if b_lo > b_hi + 1e-9:
                continue
            return n, rows, R / r_mm, min(max(n * h_mm / 8, b_lo), b_hi)
    return None


def _diagonals(u: np.ndarray, v: np.ndarray) -> list[np.ndarray]:
    """The four diagonal directions of a frame, in the order of a box's corners (-u-v, +u-v, +u+v, -u+v)."""
    return [(a * u + b * v) / math.sqrt(2.0) for a, b in ((-1, -1), (1, -1), (1, 1), (-1, 1))]


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


def _cluster(values: list[tuple[float, str]], tol: float,
             limits: dict[str, tuple[float, float]] | None = None) -> list[tuple[float, bool, list[int]]]:
    """Values along one axis ((value, kind): kind "edge" for a straight edge of the face there, else a box side's
    index) joined where neighbours are closer than tol: [(the value - an edge's where one stands -, whether an edge
    stands there, the box sides on it)]. SHMESH-48i: with `limits` ({kind: (lowest, highest)} - where each box side
    may go and still keep its ring inside its box), a value joins its neighbours' line only where one place suits them
    all (an edge only where it suits every side on it): a chain of near lines (holes out of step, each side a little
    from the next) no longer pulls a side past its ring; the line stands at the mean, moved inside every side's
    limits."""
    out: list[tuple[float, bool, list[int]]] = []
    group: list[tuple[float, str]] = []
    span = (-math.inf, math.inf)

    def fits(kind: str, val: float) -> tuple[float, float]:
        if kind == "edge":
            return (val, val)
        return (limits or {}).get(kind, (-math.inf, math.inf))

    def close() -> None:
        edges = [x for x, k in group if k == "edge"]
        at = float(np.mean(edges)) if edges else float(np.mean([x for x, _k in group]))
        if limits is not None and not edges and span[0] <= span[1]:
            at = min(max(at, span[0]), span[1])
        out.append((at, bool(edges), [int(k) for _x, k in group if k != "edge"]))

    for val, kind in sorted(values) + [(math.inf, "")]:
        if group:
            lo, hi = fits(kind, val) if math.isfinite(val) else (math.inf, -math.inf)
            both = (max(span[0], lo), min(span[1], hi))
            if val - group[-1][0] > tol or (limits is not None and both[0] > both[1]):
                close()
                group, span = [], (-math.inf, math.inf)
            else:
                span = both
        if math.isfinite(val):
            if not group:
                span = fits(kind, val)
            group.append((val, kind))
    return out


def _remove_loose(gmsh, curves: set[int]) -> None:
    """Take out the curves among these that bound no surface (pieces of a cutting line outside the face)."""
    have = {c for _d, c in gmsh.model.getEntities(1)}
    loose = [(1, c) for c in sorted(curves & have) if not len(gmsh.model.getAdjacencies(1, c)[0])]
    if loose:
        gmsh.model.occ.remove(loose, recursive=True)
        gmsh.model.occ.synchronize()


def _face_plan(gmsh, t: int, ws: list[Washer], h_mm: float, mm: float, hole_size, low_mm: float | None,
               high_mm: float | None, frame: tuple | None = None) -> dict | str:
    """SHMESH-41/47c: a face's grid plan before any line is drawn - its frame, each washer's O-grid plan, the
    straight edges and box sides along each axis, the boxes (48b: overlapping ones share a line), each box side's
    limits (48i) - or why it has none (words)."""
    h = h_mm / mm
    _l, loops = _curve_loops(gmsh, t)
    loops = [[abs(int(c)) for c in cl] for cl in loops]
    hole_loops = [sorted(w.hole) for w in ws]
    rest = [cl for cl in loops if sorted(cl) not in hole_loops]
    if len(rest) != 1:
        return "a hole without a washer in the face"
    outer = rest[0]
    # (a joined face keeps the frame it was planned in: the joints' cut split its long edges, SHMESH-47c)
    fr = frame if frame is not None else _frame(gmsh, t, outer)
    if fr is None:
        return "no straight edge to line up with"
    o, u, v = fr

    def uv(p: np.ndarray, o=o, u=u, v=v) -> tuple[float, float]:
        return float((p - o) @ u), float((p - o) @ v)

    # SHMESH-48h: a square ring (SHMESH-45f) takes a free box round its outer corners; its strips are drawn
    # in that box after the grid (imprint), so the grid is cut as for a round hole's free box
    plans = [None if w.corners else _boxed(w.r * mm, hole_size(w.r * mm, w.owner), h_mm, low_mm, high_mm)
             for w in ws]
    square_uv = {id(w): np.array([uv(q) for q in _frame_points(w)[0]]) for w in ws if w.corners}
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
    for w, p in zip(ws, plans, strict=True):
        cu, cv = uv(w.centre)
        if w.corners:
            q = square_uv[id(w)]
            boxes.append([float(q[:, 0].min()) - BOX_FREE * h, float(q[:, 0].max()) + BOX_FREE * h,
                          float(q[:, 1].min()) - BOX_FREE * h, float(q[:, 1].max()) + BOX_FREE * h])
            continue
        b = p[3] / mm if p is not None else w.R + BOX_FREE * h
        boxes.append([cu - b, cu + b, cv - b, cv + b])
    # SHMESH-48b: boxes of neighbouring holes that overlap (rows of bolt holes closer than a box) share one
    # line halfway between them - each box is trimmed to it; the room check below keeps every ring inside
    for lo_i, hi_i, olo, ohi in ((0, 1, 2, 3), (2, 3, 0, 1)):
        for j in range(len(boxes)):
            for k in range(len(boxes)):
                bj, bk = boxes[j], boxes[k]
                cj, ck = 0.5 * (bj[lo_i] + bj[hi_i]), 0.5 * (bk[lo_i] + bk[hi_i])
                side_by_side = min(bj[ohi], bk[ohi]) - max(bj[olo], bk[olo]) > 1e-9 * h
                if j != k and cj < ck and side_by_side and bj[hi_i] > bk[lo_i]:
                    mid = 0.5 * (bj[hi_i] + bk[lo_i])
                    if cj < mid < ck:
                        bj[hi_i] = bk[lo_i] = mid
    for j, bx in enumerate(boxes):
        vals["u"] += [(bx[0], str(4 * j)), (bx[1], str(4 * j + 1))]
        vals["v"] += [(bx[2], str(4 * j + 2)), (bx[3], str(4 * j + 3))]
    # SHMESH-48i: where each box side may go when near lines are made one - no nearer its ring than the room
    # check below asks, no further than SNAP from where it was, an O-grid box no longer or shorter than its
    # elements allow (each side half the slack)
    limits: dict[str, tuple[float, float]] = {}
    if SNAP_LIMITS:
        for j, (w, p, bx) in enumerate(zip(ws, plans, boxes, strict=True)):
            cu, cv = uv(w.centre)
            if w.corners:
                q = square_uv[id(w)]
                inner = [float(q[:, 0].min()), float(q[:, 0].max()), float(q[:, 1].min()),
                         float(q[:, 1].max())]
                need = 0.4 * h
            else:
                inner = [cu, cu, cv, cv]
                need = (p[2] * w.r + 0.3 * h) if p is not None else (w.R + 0.4 * h)
            need += 1e-6 * h        # (a side moved onto its limit passes the check below)
            for ax_i in (0, 1):
                lo_s, hi_s = 2 * ax_i, 2 * ax_i + 1
                grow_, shrink_ = SNAP * h, SNAP * h
                if p is not None:
                    side = bx[hi_s] - bx[lo_s]
                    grow_ = min(grow_, max(0.0, 0.5 * (1.5 * h * p[0] / 4 - side)))
                    shrink_ = min(shrink_, max(0.0, 0.5 * (side - 0.6 * h * p[0] / 4)))
                limits[str(4 * j + lo_s)] = (bx[lo_s] - grow_, min(bx[lo_s] + shrink_, inner[lo_s] - need))
                limits[str(4 * j + hi_s)] = (max(bx[hi_s] - shrink_, inner[hi_s] + need), bx[hi_s] + grow_)
    return {"h": h, "loops": loops, "outer": outer, "frame": (o, u, v), "uv": uv, "plans": plans,
            "square_uv": square_uv, "vals": vals, "lo": lo, "hi": hi, "boxes": boxes, "limits": limits}


#: SHMESH-47c: faces joined along their grids' lines (a channel's web and flanges, and the narrow flat strips between
#: them) are cut across at the same places, each line carried across the strips: else each face's lines ended on the
#: joint, cut the strip beside it on one side only, and the strip came out in triangles ("its lines cut a joined face
#: into triangles"), or a face beside another with holes was not gridded at all
JOIN = True
#: ... faces whose frames run this near one way (cosine) are one group: their lines are drawn at one place each
JOIN_COS = 1.0 - 1e-9
#: SHMESH-47d: a face beside another face with holes splits their shared edges where its lines meet them first
JOIN_BESIDE = True


def _straight(gmsh, c: int) -> tuple[np.ndarray, np.ndarray] | None:
    """The two ends of a straight curve, else None."""
    if gmsh.model.getType(1, c) != "Line":
        return None
    a, b = _samples(gmsh, c, 1)
    return a, b


def _bridge(gmsh, f: int, axis: np.ndarray, cos_par: float) -> list[int] | None:
    """SHMESH-47c: a flat face of one loop of four straight edges, two of them along the axis (a strip between
    joined faces): those two edges, else None."""
    if gmsh.model.getType(2, f) != "Plane":
        return None
    _l, loops = _curve_loops(gmsh, f)
    if len(loops) != 1 or len(loops[0]) != 4:
        return None
    along = []
    for c in (abs(int(x)) for x in loops[0]):
        seg = _straight(gmsh, c)
        if seg is None:
            return None
        d = seg[1] - seg[0]
        if abs(float(d @ axis)) >= cos_par * max(float(np.linalg.norm(d)), 1e-300):
            along.append(c)
    return along if len(along) == 2 else None


def _at_s(seg: tuple[np.ndarray, np.ndarray], axis: np.ndarray, s: float, eps: float) -> np.ndarray | None:
    """The point of a straight edge where the axis coordinate is s, when it lies inside the edge (eps from its ends)."""
    a, b = seg
    da, db = float(a @ axis), float(b @ axis)
    if abs(db - da) <= eps:
        return None
    tt = (s - da) / (db - da)
    if tt * abs(db - da) <= eps or (1.0 - tt) * abs(db - da) <= eps:
        return None
    return a + tt * (b - a)


def _remap_washers(gmsh, ws: list[Washer], new: list[int]) -> None:
    """After a cut of their face: each washer's face and hole curves, found again among the new surfaces."""
    for w in ws:
        hit = next(((x, cl) for x in new for cl in ([abs(int(c)) for c in y] for y in _curve_loops(gmsh, x)[1])
                    if (_on_square(gmsh, w, cl) if w.corners else _on_hole(gmsh, w, cl))), None)
        if hit is None:
            raise ValueError("a washer's hole was lost in the joined faces' cut")
        w.face, w.hole = hit[0], hit[1]


def _split_beside(gmsh, t: int, fp: dict, by_face: dict[int, list[Washer]], tag_of: dict[int, int]
                  ) -> tuple[int, tuple[list[float], dict[int, float], tuple]] | None:
    """SHMESH-47d: a face to grid beside another face with holes of its own (a top plate on a column's walls, a flange
    on its web): its lines would end inside the edge they share and cut it on one side only, so the face was not
    gridded ("a cell would come out too small, a ring or a shared edge cut"). The shared edges are split first, at
    every place the face's lines (made one as its grid makes them) cross them - both faces cut together, the other
    face only at those points - so its lines end on points both faces have. Returns (face, (its lines across its
    frame, {box side: where}, its frame)) - the frame it was planned in, kept as the split edges change the longest
    edge - or None when no such edge is cut."""
    o, u, v = fp["frame"]
    h = float(fp["h"])
    lines: dict[str, list[float]] = {"u": [], "v": []}
    moved: dict[int, float] = {}
    for ax in ("u", "v"):
        for at, edge, sides in _cluster(fp["vals"][ax], SNAP * h, fp["limits"] if SNAP_LIMITS else None):
            if ax == "u":
                moved.update({int(sd): at for sd in sides})
            if not edge and sides:
                lines[ax].append(at)
    eps = 1e-6 * h
    points: list[tuple[int, int]] = []
    beside: set[int] = set()
    occ = gmsh.model.occ
    for c in fp["outer"]:
        nbrs = [int(n) for n in gmsh.model.getAdjacencies(1, c)[0] if int(n) != t]
        if not any(n in by_face and n in tag_of for n in nbrs):
            continue
        seg = _straight(gmsh, c)
        if seg is None:
            continue
        for axis, ax in ((u, "u"), (v, "v")):
            for x in lines[ax]:
                p = _at_s(seg, axis, float(o @ axis) + x, eps)
                if p is not None:
                    points.append((0, occ.addPoint(*[float(q) for q in p])))
                    beside.update(n for n in nbrs if n in tag_of)
    if not points:
        return None
    objects = sorted({t} | beside)
    _o, omap = occ.fragment([(2, f) for f in objects], points)
    occ.synchronize()
    for f, got in zip(objects, omap[:len(objects)], strict=True):
        k = tag_of.pop(f)
        new = [x for d, x in got if d == 2]
        for x in new:
            tag_of[x] = k
        _remap_washers(gmsh, by_face.get(f, []), new)
    loose = [(0, p) for got in omap[len(objects):] for d, p in got
             if d == 0 and not len(gmsh.model.getAdjacencies(0, p)[0])]
    if loose:
        occ.remove(loose)
        occ.synchronize()
    k_t = next(tag_of[x] for d, x in omap[objects.index(t)] if d == 2)
    return k_t, (lines["u"], moved, (o, u, v))


def _join_faces(gmsh, by_face: dict[int, list[Washer]], tag_of: dict[int, int], size_mm: dict[int, float],
                mm: float, hole_size, low_mm: float | None, high_mm: float | None, no_cut: frozenset,
                cuts: set[int] | None, join_out: dict[int, set[int]] | None, frames: dict[int, tuple]
                ) -> dict[int, tuple[list[float], dict[int, float], tuple]]:
    """SHMESH-47c: groups of faces to grid joined along straight edges that run along their frames (directly, or
    through flat four-sided strips, _bridge): the box sides across the frame of every face of a group are made one
    (_cluster with each side's limits, SHMESH-48i) at one place along the group's axis, every face takes a line at
    every such place, each strip is cut straight across there and every joint split there - in one cut, before any
    face is gridded, so the faces' own lines end on the joints' points and no joint is cut on one side only. Returns
    {face (sewn index): (its lines across its frame, {box side: where})}; `tag_of`, the washers' faces and holes
    follow the cut."""
    cos_par = math.cos(math.radians(PARALLEL_DEG))
    plan: dict[int, dict] = {}
    for t, ws in sorted(by_face.items()):
        if t in tag_of and tag_of[t] not in no_cut:
            fp = _face_plan(gmsh, t, ws, size_mm[t], mm, hole_size, low_mm, high_mm)
            if not isinstance(fp, str):
                fp["ws"] = ws
                plan[t] = fp
    joint: dict[int, tuple[list[float], dict[int, float], tuple]] = {}
    done: set[int] = set()          # (sewn indices: a cut gives a face a new tag)
    occ = gmsh.model.occ
    big = 1_000_000

    def refresh() -> None:
        """After a cut: the faces with washers under their new tags, their plans made again in the frames they had."""
        all_ws = [w for ws_ in by_face.values() for w in ws_]
        by_face.clear()
        for w in all_ws:
            by_face.setdefault(w.face, []).append(w)
        for t_old in sorted(plan):
            fp_old = plan[t_old]
            t_new = fp_old["ws"][0].face
            if t_new == t_old:
                continue
            del plan[t_old]
            fp_new = _face_plan(gmsh, t_new, fp_old["ws"], float(fp_old["h"]) * mm, mm, hole_size, low_mm, high_mm,
                                frame=fp_old["frame"])
            if not isinstance(fp_new, str):
                fp_new["ws"] = fp_old["ws"]
                plan[t_new] = fp_new

    while True:
        t0 = next((t for t in sorted(plan) if tag_of[t] not in done), None)
        if t0 is None:
            break
        axis = plan[t0]["frame"][1]
        members: list[int] = [t0]
        bridges: dict[int, list[int]] = {}
        terminals: set[int] = set()
        junctions: set[int] = set()
        seen = {t0}
        queue = [t0]
        while queue:
            f = queue.pop(0)
            curves = plan[f]["outer"] if f in plan else bridges[f]
            for c in curves:
                seg = _straight(gmsh, c)
                if seg is None:
                    continue
                d = seg[1] - seg[0]
                if abs(float(d @ axis)) < cos_par * max(float(np.linalg.norm(d)), 1e-300):
                    continue
                for n in (int(x) for x in gmsh.model.getAdjacencies(1, c)[0]):
                    if n == f:
                        continue
                    junctions.add(c)
                    if n in seen:
                        continue
                    seen.add(n)
                    if n in plan and tag_of[n] not in done and abs(float(plan[n]["frame"][1] @ axis)) >= JOIN_COS:
                        members.append(n)
                        queue.append(n)
                        continue
                    along = _bridge(gmsh, n, axis, cos_par) if n in tag_of and n not in by_face else None
                    if along is not None:
                        bridges[n] = along
                        queue.append(n)
                    elif n in tag_of:
                        terminals.add(n)
        done |= {tag_of[f] for f in members}
        if len(members) < 2 and not bridges:
            if JOIN_BESIDE:
                got = _split_beside(gmsh, t0, plan[t0], by_face, tag_of)
                if got is not None:
                    joint[got[0]] = got[1]
                    refresh()
            continue
        members.sort()
        h = min(float(plan[f]["h"]) for f in members)
        vals: list[tuple[float, str]] = []
        lim: dict[str, tuple[float, float]] = {}
        frame_of: dict[int, tuple[float, float]] = {}
        for fi, f in enumerate(members):
            o, u, _v = plan[f]["frame"]
            sg = 1.0 if float(u @ axis) > 0 else -1.0
            off = float(o @ axis)
            frame_of[f] = (off, sg)
            for x, kind in plan[f]["vals"]["u"]:
                if kind == "edge":
                    vals.append((off + sg * x, "edge"))
                    continue
                key = str(fi * big + int(kind))
                vals.append((off + sg * x, key))
                if kind in plan[f]["limits"]:
                    a, b = plan[f]["limits"][kind]
                    lim[key] = (off + a, off + b) if sg > 0 else (off - b, off - a)
        for along in bridges.values():
            for c in along:
                seg = _straight(gmsh, c)
                if seg is not None:
                    vals += [(float(seg[0] @ axis), "edge"), (float(seg[1] @ axis), "edge")]
        out = _cluster(vals, SNAP * h, lim if SNAP_LIMITS else None)
        places = [at for at, _e, sides in out if sides]
        for fi, f in enumerate(members):
            off, sg = frame_of[f]
            hf = float(plan[f]["h"])
            own = [x for x, k in plan[f]["vals"]["u"] if k == "edge"]
            lo_x, hi_x = float(plan[f]["lo"][0]), float(plan[f]["hi"][0])
            moved = {int(k) % big: (at - off) * sg for at, _e, sides in out for k in sides if int(k) // big == fi}
            xs = sorted(x for x in ((at - off) * sg for at in places)
                        if lo_x + 1e-6 * hf < x < hi_x - 1e-6 * hf and not any(abs(x - e) <= 1e-3 * hf for e in own))
            joint[tag_of[f]] = (xs, moved, plan[f]["frame"])
        # the cut: each strip straight across at every place, every other joint split there
        eps = 1e-6 * h
        segs: list[tuple[int, int]] = []
        points: list[tuple[int, int]] = []
        strip_edges = {c for along in bridges.values() for c in along}
        for s in places:
            for along in bridges.values():
                ends = [_at_s(sg_, axis, s, eps) for sg_ in (_straight(gmsh, c) for c in along) if sg_ is not None]
                if len(ends) == 2 and ends[0] is not None and ends[1] is not None:
                    segs.append((1, occ.addLine(occ.addPoint(*[float(x) for x in ends[0]]),
                                                occ.addPoint(*[float(x) for x in ends[1]]))))
            for c in sorted(junctions - strip_edges):
                seg = _straight(gmsh, c)
                p = _at_s(seg, axis, s, eps) if seg is not None else None
                if p is not None:
                    points.append((0, occ.addPoint(*[float(x) for x in p])))
        if not segs and not points:
            continue
        objects = sorted(set(members) | set(bridges) | terminals)
        owner = {f: tag_of[f] for f in objects}
        _o, omap = occ.fragment([(2, f) for f in objects], segs + points)
        occ.synchronize()
        for f, got in zip(objects, omap[:len(objects)], strict=True):
            k = tag_of.pop(f)
            new = [x for d, x in got if d == 2]
            for x in new:
                tag_of[x] = k
            _remap_washers(gmsh, by_face.get(f, []), new)
        refresh()
        drawn ={c for got in omap[len(objects):len(objects) + len(segs)] for d, c in got if d == 1}
        _remove_loose(gmsh, drawn)
        have = {c for _d, c in gmsh.model.getEntities(1)}
        if cuts is not None:
            cuts.update(drawn & have)
        loose = [(0, p) for got in omap[len(objects) + len(segs):] for d, p in got
                 if d == 0 and not len(gmsh.model.getAdjacencies(0, p)[0])]
        if loose:
            occ.remove(loose)
            occ.synchronize()
        if join_out is not None:
            # the strips cut with the group: no triangle there on a first mesh, else the group's faces are not
            # gridded (SHMESH-47b)
            strips = {owner[f] for f in bridges}
            for f in members:
                join_out.setdefault(owner[f], set()).update(strips)
    for fp_ in plan.values():
        frames.setdefault(tag_of[fp_["ws"][0].face], fp_["frame"])
    return joint


def block_faces(gmsh, washers: list[Washer], tag_of: dict[int, int], size_mm: dict[int, float], hole_size, mm: float,
                cuts: set[int] | None = None, low_mm: float | None = None, high_mm: float | None = None,
                cells_out: set[int] | None = None, no_cut: frozenset = frozenset(),
                cut_out: dict[int, set[int]] | None = None, made_out: set[int] | None = None,
                why_where: dict[str, list[int]] | None = None, join_out: dict[int, set[int]] | None = None
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
        if why_where is not None:
            # the record says which face each reason came from (SHMESH-48f)
            why_where.setdefault(reason, []).append(int(tag_of.get(t, -1)))

    frames: dict[int, tuple] = {}       # (SHMESH-47d: each face keeps the frame it was planned in)
    joint: dict[int, tuple[list[float], dict[int, float], tuple]] = {}
    if JOIN:
        # SHMESH-47c: faces joined along their frames' lines (a channel's web and flanges, through the narrow strips
        # between them) are cut across at the same places - lines carried across the strips - before each is gridded
        try:
            joint = _join_faces(gmsh, by_face, tag_of, size_mm, mm, hole_size, low_mm, high_mm, no_cut, cuts,
                                join_out, frames)
        except Exception as exc:  # noqa: BLE001 - the faces are then gridded one by one, as before
            joint = {}
            why[f"its joined faces were not cut ({type(exc).__name__}: {exc})"[:200]] = 1
        by_face = {}
        for w in washers:
            by_face.setdefault(w.face, []).append(w)
    for t, ws in sorted(by_face.items()):
        if tag_of.get(t) in no_cut:
            # SHMESH-47b/48f: its grid broke a line or left triangles on a first mesh (there or in a face it cut)
            no("its grid left triangles or an unmet line on a first mesh")
            continue
        # SHMESH-45g: a face whose grid fails in any way loses only its own grid (its washers go on to the free
        # rings, or to none where the face was already cut); the other faces keep theirs
        try:
            h_mm = size_mm[t]
            jt = joint.get(tag_of[t])
            fp = _face_plan(gmsh, t, ws, h_mm, mm, hole_size, low_mm, high_mm,
                            frame=jt[2] if jt else frames.get(tag_of[t]))
            if isinstance(fp, str):
                no(fp)
                continue
            h, loops, (o, u, v), uv = fp["h"], fp["loops"], fp["frame"], fp["uv"]
            plans, square_uv, vals, lo, hi = fp["plans"], fp["square_uv"], fp["vals"], fp["lo"], fp["hi"]
            boxes, limits = fp["boxes"], fp["limits"]
            lines: dict[str, list[float]] = {"u": [], "v": []}
            for ax in ("u", "v"):
                if ax == "u" and jt is not None:
                    # SHMESH-47c: a face joined to others along its frame's lines takes the lines across them all
                    lines["u"] = list(jt[0])
                    for sd, at in jt[1].items():
                        boxes[sd // 4][sd % 4] = at
                    continue
                for at, edge, sides in _cluster(vals[ax], SNAP * h, limits if SNAP_LIMITS else None):
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
                if w.corners:
                    # a square ring's room: from its outer corners to the box's sides
                    q = square_uv[id(w)]
                    R, room = 0.0, min(float(q[:, 0].min()) - bx[0], bx[1] - float(q[:, 0].max()),
                                       float(q[:, 1].min()) - bx[2], bx[3] - float(q[:, 1].max()))
                if room < R + (0.3 if p is not None else 0.4) * h:
                    reason = "a ring too near an edge or another ring for its box"
                elif p is not None and not all(0.6 * h <= x / (p[0] / 4) <= 1.5 * h
                                               for x in (bx[1] - bx[0], bx[3] - bx[2])):
                    reason = "a box drawn on to an edge comes out too long or short for its ring"
                elif not STOP_AT_BOXES and (any(bx[0] + eps < x < bx[1] - eps for x in lines["u"]) or any(
                        bx[2] + eps < y < bx[3] - eps for y in lines["v"])):
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
            for w, p in zip(ws, plans, strict=True):
                if p is not None:
                    starts[id(w)] = _diagonals(u, v)[2]          # (u + v) / sqrt 2: where SHMESH-45b starts the hole

            def line_pieces(at: float, axis_: int, start: float, stop: float, boxes=boxes,
                       eps=eps) -> list[tuple[float, float]]:
                """SHMESH-48g: a line across the face, stopped at each box it would cross (it ends on that box's
                side line, drawn right across: a T, the cells four-sided) - holes in rows that do not line up."""
                cut_at = sorted((bx[2 - 2 * axis_], bx[3 - 2 * axis_]) for bx in boxes
                                if bx[2 * axis_] + eps < at < bx[2 * axis_ + 1] - eps) if STOP_AT_BOXES else []
                out_, at0 = [], start
                for g0, g1 in cut_at:
                    if g0 > at0 + eps:
                        out_.append((at0, g0))
                    at0 = max(at0, g1)
                if stop > at0 + eps:
                    out_.append((at0, stop))
                return out_

            def tools(o=o, u=u, v=v, lo=lo, hi=hi, pad=pad, lines=lines, ws=ws, plans=plans, corners_of=corners_of,
                      line_pieces=line_pieces,
                      starts=starts, n_face=n_face) -> list[tuple[int, int]]:
                out = []
                occ = gmsh.model.occ
                for x in lines["u"]:
                    for y0, y1 in line_pieces(x, 0, lo[1] - pad, hi[1] + pad):
                        a, b = o + x * u + y0 * v, o + x * u + y1 * v
                        out.append((1, occ.addLine(occ.addPoint(*a), occ.addPoint(*b))))
                for y in lines["v"]:
                    for x0, x1 in line_pieces(y, 1, lo[0] - pad, hi[0] + pad):
                        a, b = o + x0 * u + y * v, o + x1 * u + y * v
                        out.append((1, occ.addLine(occ.addPoint(*a), occ.addPoint(*b))))
                for w, p, cs in zip(ws, plans, corners_of, strict=True):
                    if w.corners:
                        continue            # (a square ring is drawn in its box after the grid)
                    R = p[2] * w.r if p is not None else w.R
                    xa = starts[id(w)] if p is not None else w.xaxis
                    out.append((1, occ.addCircle(*[float(x) for x in w.centre], float(R),
                                                 zAxis=[float(x) for x in n_face], xAxis=[float(x) for x in xa])))
                    if p is not None:
                        # SHMESH-48: each box corner joined to the ring and the hole at the frame's own diagonal (where the
                        # hole's circle starts, SHMESH-45b), so the ring's quarters are exact and no seam sits in a quarter
                        for cp, dg in zip(cs, _diagonals(u, v), strict=True):
                            q1 = occ.addPoint(*[float(x) for x in w.centre + R * dg])
                            out.append((1, occ.addLine(occ.addPoint(*cp), q1)))
                            out.append((1, occ.addLine(q1, occ.addPoint(*[float(x) for x in w.centre + 0.5 * w.r * dg]))))
                return out

            # tried on a copy of the face first: no edge of a cell under CELL_MIN face sizes
            shared = [c for cl in loops for c in cl if len(gmsh.model.getAdjacencies(1, c)[0]) > 1]
            copy = gmsh.model.occ.copy([(2, t)])
            _o, cmap = gmsh.model.occ.fragment(copy, tools())
            gmsh.model.occ.synchronize()
            cells = [s for d, s in cmap[0] if d == 2]
            cut_curves = {c for got in cmap[1:] for d, c in got if d == 1}
            # SHMESH-48j: which of the three causes fired, with its numbers, goes in the reason
            causes: list[str] = []
            shortest = min(((float(gmsh.model.occ.getMass(1, abs(c))), abs(int(c))) for s in cells
                            for _d, c in gmsh.model.getBoundary([(2, s)], oriented=False)), default=(math.inf, 0))
            small = shortest[0] < CELL_MIN * h
            if small:
                mid_ = _samples(gmsh, shortest[1], 2)[1] * mm
                causes.append(f"a cell edge {shortest[0] * mm:.3g} mm, under {CELL_MIN * h_mm:.3g} mm, at "
                              f"({mid_[0]:.0f}, {mid_[1]:.0f}, {mid_[2]:.0f}) mm")
            # and every ring came out as drawn: one circle round a free box's hole, four quarters round an O-grid's
            trial = sorted({abs(int(c)) for s in cells for _d, c in gmsh.model.getBoundary([(2, s)], oriented=False)})
            # a line that ends inside an edge the face shares with another face cuts that edge: the other face is cut
            # with it (at that point only), else it would keep the edge whole - a crack at the junction
            hit: set[int] = set()
            if shared:
                from scipy.spatial import cKDTree

                for c in shared:
                    tree = cKDTree(_samples(gmsh, c, 256))
                    full = float(gmsh.model.occ.getMass(1, c))
                    for x in trial:
                        if tree.query(_samples(gmsh, x, 4))[0].max() <= 1e-3 * h + full / 256 and                             float(gmsh.model.occ.getMass(1, x)) < (1 - 1e-6) * full:
                            hit.add(c)
            # the pieces of the lines inside the face (straight: their two ends), to cut it and its neighbours with
            outline = cKDTree(np.vstack([_samples(gmsh, c, 64) for cl in loops for c in cl])) if hit else None
            pieces: list[tuple[np.ndarray, np.ndarray]] = []
            if hit and outline is not None:
                for x in trial:
                    if gmsh.model.getType(1, x) != "Line":
                        continue
                    q = _samples(gmsh, x, 4)
                    if outline.query(q[2])[0] > 1e-3 * h:
                        pieces.append((q[0], q[-1]))
            spoilt = False
            if hit and (not CUT_NEIGHBOURS or tag_of[t] in no_cut):
                # a line would cut a shared edge (the neighbour would keep it whole: a crack) - and cutting the
                # neighbour is off, or it left poor elements there on a first mesh (SHMESH-47b)
                small = True
                spoilt = CUT_NEIGHBOURS
            others = sorted({int(n) for c in hit for n in gmsh.model.getAdjacencies(1, c)[0] if int(n) != t})
            if any(n in by_face or n not in tag_of for n in others) or any(w.face in others for w in washers):
                small = True        # a neighbour with washers of its own is left as it is
                causes.append("a line would cut an edge shared with a face with holes of its own")
            elif hit and not spoilt and small and not causes:
                causes.append("a line would cut a shared edge")
            for w, p in zip(ws, plans, strict=True):
                if w.corners:
                    continue
                R = p[2] * w.r if p is not None else w.R
                on_ring = [c for c in trial if np.all(np.abs(np.linalg.norm(_samples(gmsh, c, 4) - w.centre, axis=1) - R)
                                                      <= 1e-4 * R)]
                if len(on_ring) != (4 if p is not None else 1):
                    small = True
                    if not any(x.startswith("a ring") for x in causes):
                        c_ = w.centre * mm
                        causes.append(f"a ring came out in {len(on_ring)} pieces, not {4 if p is not None else 1}, "
                                      f"round the {2 * w.r * mm:.3g} mm hole at ({c_[0]:.0f}, {c_[1]:.0f}, "
                                      f"{c_[2]:.0f}) mm")
            gmsh.model.occ.remove([(2, s) for s in cells], recursive=True)
            gmsh.model.occ.synchronize()
            _remove_loose(gmsh, cut_curves)
            if small:
                no("its lines cut a joined face into poor elements on a first mesh" if spoilt
                   else "a cell would come out too small, a ring or a shared edge cut: " + "; ".join(causes))
                continue
            edge_pts = np.vstack([_samples(gmsh, c, 64) for cl in loops for c in cl])
            if others:
                if cut_out is not None:
                    cut_out.setdefault(tag_of[t], set()).update(tag_of[n] for n in others)
                occ = gmsh.model.occ
                cutters = [(1, occ.addLine(occ.addPoint(*[float(v) for v in pa]), occ.addPoint(*[float(v) for v in pb])))
                           for pa, pb in pieces]
                circles: list[tuple[int, int]] = []
                # (the rings and the cuts from the box corners to the holes are straight lines and circles inside the
                # face: drawn again, with the pieces of the box lines)
                for w, p in zip(ws, plans, strict=True):
                    if w.corners:
                        continue
                    R = p[2] * w.r if p is not None else w.R
                    xa = starts[id(w)] if p is not None else w.xaxis
                    circles.append((1, occ.addCircle(*[float(x) for x in w.centre], float(R),
                                                     zAxis=[float(x) for x in n_face], xAxis=[float(x) for x in xa])))
                _o, omap = occ.fragment([(2, t)] + [(2, n) for n in others], cutters + circles)
                occ.synchronize()
                for j, n in enumerate(others, start=1):
                    kn = tag_of.pop(n)
                    for d, s2 in omap[j]:
                        if d == 2:
                            tag_of[s2] = kn
                omap = [omap[0]] + omap[1 + len(others):]
            else:
                _o, omap = gmsh.model.occ.fragment([(2, t)], tools())
                gmsh.model.occ.synchronize()
            k = tag_of.pop(t)
            new = [s for d, s in omap[0] if d == 2]
            for s in new:
                tag_of[s] = k
            if cells_out is not None:
                cells_out.update(new)
            if made_out is not None:
                made_out.add(k)
            _remove_loose(gmsh, {c for got in omap[1:] for d, c in got if d == 1})
            curves = sorted({abs(int(c)) for s in new for _d, c in gmsh.model.getBoundary([(2, s)], oriented=False)})
            on_circle: set[int] = set()
            for w in ws:
                if w.corners:
                    # SHMESH-48h: the square hole now sits in its box's cell: the ring is drawn there (imprint)
                    cell = next(((s, cl) for s in new for cl in [[abs(int(c)) for c in x]
                                                                 for x in _curve_loops(gmsh, s)[1]]
                                 if _on_square(gmsh, w, cl)), None)
                    if cell is None:
                        raise ValueError("a square hole was not found in its box")
                    w.face, w.hole, w.in_box = cell[0], cell[1], True
            for w, p in zip(ws, plans, strict=True):
                if w.corners:
                    continue
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
                        _l3, lp = _curve_loops(gmsh, s)
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


def reseat_holes(brep, out, only: list | None = None, seam: list | None = None) -> tuple[int, list]:
    """SHMESH-45b: every round hole of a flat face that no other face shares is drawn again as ONE circle starting on the
    diagonal of the face's frame (its longest straight outer edge, as block_faces reads it), the same circle exactly. A
    CAD hole's circle starts at its seam, often in the middle of an O-grid quarter, and the quarter then cannot be
    counted evenly; drawn again, the seam sits where the O-grid cuts anyway. Only the hole's own wire changes (edges
    shared with other faces are untouched, so every junction stays as it is); a face whose area would change is left
    as it was. Writes `out` and returns (how many holes were drawn again - 0: `out` is the input unchanged -, [(each
    one's centre, how many CAD edges it had)]).

    SHMESH-45i/49c: a hole that is one exact circle, or exact halves or quarters, keeps the CAD's edges (its ring is in
    step; moving only the seam changed gmsh's free paving between rings) - unless its centre is in `seam`: the O-grid
    holes of a first mesh, drawn again to start where their O-grid cuts (the face frame's diagonal, or 45 degrees to
    a round plate's radial line)."""
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
        # SHMESH-49b: on a round plate (its outer edge one circle) each hole starts at 45 degrees to the line from the
        # plate's centre - where polar_faces' O-grid cuts it
        round_c = None
        outer_es = edges(outer)
        if diag is None and len(outer_es) == 1:
            oc = BRepAdaptor_Curve(outer_es[0])
            if oc.GetType() == GeomAbs_Circle:
                lo_ = oc.Circle().Location()
                round_c = np.array([lo_.X(), lo_.Y(), lo_.Z()])
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
            if only is not None and not any(
                    float(np.linalg.norm(np.asarray(c_, dtype=float) - np.asarray(loc, dtype=float))) <= 0.25 * radius
                    for c_ in only):
                continue            # SHMESH-45k: only the holes a first mesh's rings asked for
            circs = [BRepAdaptor_Curve(e) for e in es]
            exact = all(cv.GetType() == GeomAbs_Circle for cv in circs)
            p0 = circs[0].Value(circs[0].FirstParameter())
            rel = np.array([p0.X() - loc[0], p0.Y() - loc[1], p0.Z() - loc[2]])
            rel /= max(float(np.linalg.norm(rel)), 1e-300)
            want = diag
            if want is None and round_c is not None:
                rad = np.asarray(loc, dtype=float) - round_c
                rad = rad - float(rad @ nrm) * nrm
                if float(np.linalg.norm(rad)) > 1e-9 * max(1.0, float(radius)):
                    rad /= float(np.linalg.norm(rad))
                    want = (rad + np.cross(nrm, rad)) / math.sqrt(2.0)
            # (a seam entry is a centre, or (centre, the direction to start at))
            forced = [e for e in (seam or []) if float(np.linalg.norm(
                np.asarray(e[0] if len(e) == 2 else e, dtype=float) - np.asarray(loc, dtype=float))) <= 0.25 * radius]
            force = bool(forced)
            if forced and len(forced[0]) == 2:
                want = np.asarray(forced[0][1], dtype=float)
            spans = [cv.LastParameter() - cv.FirstParameter() for cv in circs] if exact else []
            if not force and exact and len(es) in (2, 4) and max(spans) - min(spans) <= 1e-6 * 2 * math.pi:
                continue            # exact halves or quarters: in step with any ring
            if len(es) == 1 and exact and (not force or want is None or float(rel @ want) > 1 - 1e-9):
                continue            # already one circle (an O-grid's starting where it cuts)
            start = want if want is not None else rel
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


#: SHMESH-49: a round flat plate with its holes on one bolt circle is cut into rings and spokes: two circles round the
#: bolt circle (BOX_FREE face sizes clear of the washers' rings), a spoke each side of every hole and one in the middle
#: of a gap wider than POLAR_GAP face sizes, the middle of a full disc left whole (paved: a mapped core square would take
#: a quarter circle's count on a side 0.45 times as long). The sectors are four-sided (mapped); each washer keeps its ring in its own
#: sector, paved from ring to sector. Holes on one circle within POLAR_ROUND of its radius, spaced alike within
#: POLAR_EVEN times.
POLAR = True
#: SHMESH-49g: a round plate whose rings and strips leave triangles or poor elements is meshed again without them
POLAR_CHECK = True
POLAR_TRIAS = 4
#: SHMESH-49g: ... or this share of its elements, when every one meets the tria angle lines
POLAR_TRIA_SHARE = 0.01
#: SHMESH-49f: each hole's box square (straight across its strip) or bounded by the bolt circle's rings (arcs)
POLAR_SQUARE = True
#: SHMESH-49h: each box side bowed out by this many degrees at its ends: a square box's corner splits into two 45 degree
#: O-grid corners, on the quad-angle line (split into triangles by the clean-up); bowed, 45 plus this
POLAR_BOW = 12.0
#: SHMESH-49i: spokes run on inside the bolt circle's inner ring to a central hole (off: that band is one paved ring - a
#: count running from the central hole to the rim could not keep both its ends inside the user's edges)
POLAR_INNER_SPOKES = False
POLAR_ROUND = 0.02
POLAR_EVEN = 1.5
POLAR_GAP = 3.0


def _round_loop(gmsh, curves: list[int]) -> tuple[np.ndarray, float] | None:
    """(centre, radius) when a loop is one circle (in any number of arcs), else None."""
    got = _circle_of(gmsh, curves)
    return (got[0], got[1]) if got is not None else None


@dataclass
class Band:
    """SHMESH-49l: the band of a round plate between its central opening and its bolt circle's inner ring."""
    face: int                       # the cell (gmsh surface)
    owner: int                      # the sewn face
    centre: np.ndarray
    normal: np.ndarray
    inner: list[int]                # the opening's curves
    outer: list[int]                # the ring's pieces (arcs between strips, box sides across them)
    r_in: float
    n_in: int = 0


#: SHMESH-49l: the band inside a bolt circle (round a central opening) meshed in rings of quads, the count dropping
#: from the ring's to the opening's in one row of 3-to-1 groups (none: paved by gmsh). Off: the 3-to-1 quads come out
#: with a taper of 0.50-0.57 against Nastran's 0.5 and the clean-up splits them (60-104 triangles on a look-alike plate)
POLAR_BAND = False
BAND_SMOOTH = 5
BAND_AB_DEPTH = 0.65
BAND_AB_SPREAD = 0.25


def band_counts(gmsh, band: Band, known: dict[int, int], size_mm: float, mm: float) -> int | None:
    """SHMESH-49l: the count round the band's opening (set on its curves), the ring's count less two for each 3-to-1
    group so the opening's elements come out near the ring's in length; None when the ring is not all counted."""
    if any(c not in known for c in band.outer):
        return None
    n_out = sum(known[c] for c in band.outer)
    r_out = float(np.mean([np.linalg.norm(_samples(gmsh, c, 4) - band.centre, axis=1).mean() for c in band.outer]))
    want = n_out * band.r_in / max(r_out, 1e-300)
    n_in = n_out - 2 * max(0, round((n_out - want) / 2))
    n_in = max(n_in, n_out - 2 * (n_out // 3), 8)
    if (n_out - n_in) % 2:
        n_in -= 1
    lengths = [float(gmsh.model.occ.getMass(1, c)) for c in band.inner]
    spread = _spread(n_in, lengths) if len(band.inner) > 1 else [n_in]
    if spread is None:
        return None
    for c, m in zip(band.inner, spread, strict=True):
        gmsh.model.mesh.setTransfiniteCurve(c, m + 1)
    band.n_in = n_in
    return n_in


def band_mesh(gmsh, band: Band, index: np.ndarray, xyz: np.ndarray, mm: float, h_mm: float
              ) -> tuple[np.ndarray, np.ndarray]:
    """SHMESH-49l: the band's own quads - rings from the bolt circle's inner ring in to the opening, one row of 3-to-1
    groups (four quads round two inner nodes each) spread round it. Returns (new points, quads in xyz indices: the
    new points numbered after xyz). ValueError when the counts or the quads do not come out."""
    def ring_nodes(curves: list[int]) -> np.ndarray:
        got: set[int] = set()
        for c in curves:
            tags, _x, _p = gmsh.model.mesh.getNodes(1, c, includeBoundary=True)
            got |= set(index[np.asarray(tags, dtype=np.int64)].tolist())
        return np.array(sorted(got), dtype=np.int64)

    C = band.centre * mm
    n = band.normal / max(float(np.linalg.norm(band.normal)), 1e-300)
    outer, inner = ring_nodes(band.outer), ring_nodes(band.inner)
    if len(inner) != band.n_in or len(outer) <= len(inner) or (len(outer) - len(inner)) % 2:
        raise ValueError(f"the band's rings carry {len(outer)} and {len(inner)} nodes")
    e1 = xyz[outer[0]] - C
    e1 = e1 - float(e1 @ n) * n
    e1 /= max(float(np.linalg.norm(e1)), 1e-300)
    e2 = np.cross(n, e1)

    def polar(ids: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        d = xyz[ids] - C
        a = np.arctan2(d @ e2, d @ e1) % (2 * math.pi)
        o = np.argsort(a)
        return ids[o], a[o], np.linalg.norm(d[o] - np.outer(d[o] @ n, n), axis=1)

    outer, a_out, r_out = polar(outer)
    inner, a_in, _r_in = polar(inner)
    n_out, n_in = len(outer), len(inner)
    groups = (n_out - n_in) // 2
    # the outer segments that start a 3-to-1 group: spread evenly round the ring
    starts = sorted({int(round(g * n_out / groups)) % n_out for g in range(groups)}) if groups else []
    if len(starts) != groups or any((b - a) % n_out < 3 for a, b in zip(starts, starts[1:] + starts[:1], strict=True)
                                    if groups > 1):
        raise ValueError("the band's 3-to-1 groups do not fit round its ring")
    rows = max(2, int(round(float(np.mean(r_out) - band.r_in * mm) / h_mm)))
    t_row = rows // 2                   # the transition between ring t_row and ring t_row + 1
    # the angles each ring's nodes sit at: the ring's own above the transition, the kept ones below it, eased to the
    # opening's own at the opening
    kept = [j for j in range(n_out) if not any((j - s0) % n_out in (1, 2) for s0 in starts)]
    a_kept = a_out[kept]
    shift = int(np.argmin(np.abs(((a_in - a_kept[0] + math.pi) % (2 * math.pi)) - math.pi)))
    inner, a_in = np.roll(inner, -shift), np.roll(a_in, -shift)
    a_in_u = a_kept + ((a_in - a_kept + math.pi) % (2 * math.pi) - math.pi)
    pts: list[np.ndarray] = []
    base = len(xyz)

    def at(radius: np.ndarray, ang: np.ndarray) -> np.ndarray:
        return C + radius[:, None] * (np.cos(ang)[:, None] * e1 + np.sin(ang)[:, None] * e2)

    def add(p_: np.ndarray) -> np.ndarray:
        start = base + sum(len(x) for x in pts)
        pts.append(p_)
        return np.arange(start, start + len(p_))

    r_in = band.r_in * mm
    rings: list[np.ndarray] = [outer]
    for i in range(1, t_row + 1):
        f = i / rows
        rings.append(add(at((1 - f) * r_out + f * r_in, a_out)))
    depth = (t_row + BAND_AB_DEPTH) / rows
    mid_r = (1 - depth) * r_out + depth * r_in
    ab = {}
    for s0 in starts:
        # the group's two inner nodes spread over its span and set low in the row (top side to bottom about 1.5 to
        # 3: a middle quad at the outer nodes' own angles came out tapered past the solver's limits)
        j0, j1, j2, j3 = ((s0 + q) % n_out for q in range(4))
        span = (a_out[j3] - a_out[j0]) % (2 * math.pi)
        aa = np.array([a_out[j0] + BAND_AB_SPREAD * span, a_out[j0] + (1 - BAND_AB_SPREAD) * span])
        ab[s0] = add(at(np.array([mid_r[j1], mid_r[j2]]), aa))
    lower: list[np.ndarray] = []
    for i in range(t_row + 1, rows):
        f = (i - t_row - 1) / max(rows - t_row - 1, 1)
        ang = (1 - f) * a_kept + f * a_in_u
        r_here = (1 - i / rows) * r_out[kept] + i / rows * r_in
        lower.append(add(at(r_here, ang)))
    lower.append(inner)
    quads: list[list[int]] = []
    for i in range(t_row):
        up, dn = rings[i], rings[i + 1]
        for j in range(n_out):
            quads.append([up[j], up[(j + 1) % n_out], dn[(j + 1) % n_out], dn[j]])
    up, dn = rings[t_row], lower[0]
    pos = {j: q for q, j in enumerate(kept)}
    j = 0
    while j < n_out:
        if j in ab:
            a_, b_ = ab[j]
            o0, o1, o2, o3 = (up[(j + q) % n_out] for q in range(4))
            i0, i1 = dn[pos[j]], dn[pos[(j + 3) % n_out]]
            quads += [[o0, o1, a_, i0], [o1, o2, b_, a_], [o2, o3, i1, b_], [a_, b_, i1, i0]]
            j += 3
        else:
            quads.append([up[j], up[(j + 1) % n_out], dn[pos[(j + 1) % n_out]], dn[pos[j]]])
            j += 1
    for i in range(len(lower) - 1):
        up, dn = lower[i], lower[i + 1]
        for q in range(n_in):
            quads.append([up[q], up[(q + 1) % n_in], dn[(q + 1) % n_in], dn[q]])
    Q = np.array(quads, dtype=np.int64)
    P = np.vstack(pts) if pts else np.zeros((0, 3))
    allx = np.vstack([xyz, P])
    # a light smoothing of the band's own new nodes (each to the mean of its neighbours, in the plate's plane)
    nb: dict[int, set[int]] = {}
    for q in Q:
        for k_ in range(4):
            nb.setdefault(int(q[k_]), set()).update((int(q[k_ - 1]), int(q[(k_ + 1) % 4])))
    free = [v for v in nb if v >= base]
    for _ in range(BAND_SMOOTH):
        for v in free:
            m_ = allx[list(nb[v])].mean(axis=0)
            allx[v] = m_ - float((m_ - C) @ n) * n
    # turned as the plate's normal
    nq = np.cross(allx[Q[:, 1]] - allx[Q[:, 0]], allx[Q[:, 3]] - allx[Q[:, 0]])
    flip = nq @ n < 0
    Q[flip] = Q[flip][:, ::-1]
    return allx[base:], Q


def polar_faces(gmsh, washers: list[Washer], tag_of: dict[int, int], size_mm: dict[int, float], mm: float,
                cuts: set[int] | None = None, cells_out: set[int] | None = None, hole_size=None,
                low_mm: float | None = None, high_mm: float | None = None, no_polar: frozenset = frozenset(),
                made_out: set[int] | None = None, bands_out: list | None = None,
                why_where: dict[str, list[int]] | None = None
                ) -> tuple[list[Washer], dict[str, int], list[Washer], dict[int, int], set[int]]:
    """SHMESH-49: the round flat faces whose holes all have washers on one bolt circle, cut into rings and spokes (see
    POLAR). A face sharing an edge a line would end on, or whose cells would come out under CELL_MIN face sizes, or
    whose rings would be cut, is left as it is (tried on a copy first). Returns (the washers so made - each with its
    ring and annulus drawn, in a free sector -, {why a round face was not cut: count}, the O-grid washers, {curve:
    count} their grid must keep, their holes' curves). SHMESH-49b: where every hole takes the same O-grid plan
    (_boxed, inside the user's edges), each sector is that plan's box and its four corners are joined to the ring and
    the hole, so the sector is four mapped cells and its annulus four more - no free paving round a hole."""
    by_face: dict[int, list[Washer]] = {}
    for w in washers:
        by_face.setdefault(w.face, []).append(w)
    made: list[Washer] = []
    ogrid: list[Washer] = []
    preset: dict[int, int] = {}
    holes: set[int] = set()
    why: dict[str, int] = {}

    def no(reason: str) -> None:
        why[reason] = why.get(reason, 0) + 1
        if why_where is not None:
            # the record says which face each reason came from (SHMESH-48f)
            why_where.setdefault(reason, []).append(int(tag_of.get(t, -1)))

    occ = gmsh.model.occ
    for t, ws in sorted(by_face.items()):
        _l, loops = _curve_loops(gmsh, t)
        loops = [[abs(int(c)) for c in cl] for cl in loops]
        hole_loops = [sorted(w.hole) for w in ws]
        rest = [cl for cl in loops if sorted(cl) not in hole_loops]
        length = [sum(float(occ.getMass(1, c)) for c in cl) for cl in rest]
        if not rest:
            continue
        outer = rest[int(np.argmax(length))]
        rim = _round_loop(gmsh, outer)
        if rim is None or len(ws) < 3:
            continue                                    # not a round plate: the rectangular grid's (block_faces)
        if tag_of.get(t) in no_polar:
            # SHMESH-49g: its rings and strips left more than POLAR_TRIAS triangles on a first mesh
            no("its rings and strips left triangles on a first mesh")
            continue
        C, Ro = rim
        inner = [cl for cl in rest if cl is not outer]
        Ri = 0.0
        inner_shared = False
        if inner:
            core = _round_loop(gmsh, inner[0]) if len(inner) == 1 else None
            if core is None or float(np.linalg.norm(core[0] - C)) > 1e-3 * Ro:
                no("a round plate with another hole not on its centre")
                continue
            Ri = core[1]
            # (a central circle another face shares - a tube welded on - stays whole: the band inside the bolt
            # circle's inner ring is then left one annulus, paved)
            inner_shared = any(len(gmsh.model.getAdjacencies(1, c)[0]) > 1 for c in inner[0])
        h = size_mm[t] / mm
        n_face = ws[0].normal
        e1 = ws[0].centre - C
        e1 = e1 - float(e1 @ n_face) * n_face
        e1 /= max(float(np.linalg.norm(e1)), 1e-300)
        e2 = np.cross(n_face, e1)
        rb = [float(np.linalg.norm(w.centre - C)) for w in ws]
        Rb = float(np.mean(rb))
        if max(abs(x - Rb) for x in rb) > POLAR_ROUND * Rb:
            no("the holes are not on one bolt circle")
            continue
        ang = sorted((math.atan2(float((w.centre - C) @ e2), float((w.centre - C) @ e1)) % (2 * math.pi), w)
                     for w in ws)
        gaps = [((ang[(i + 1) % len(ang)][0] - ang[i][0]) % (2 * math.pi)) for i in range(len(ang))]
        if max(gaps) > POLAR_EVEN * min(gaps):
            no("the holes are not spaced alike round their circle")
            continue
        # one plan for holes of one size (a hole drawn again comes out a hair off its radius: one plan each split the
        # bolt circle between an O-grid and none)
        radii = [w.r for w in ws]
        plans: list[tuple[int, int, float, float] | None]
        if hole_size is None:
            plans = [None] * len(ws)
        elif max(radii) <= 1.01 * min(radii):
            r_m = float(np.mean(radii))
            one = _boxed(r_m * mm, hole_size(r_m * mm, ws[0].owner), size_mm[t], low_mm, high_mm)
            plans = [one] * len(ws)
        else:
            plans = [_boxed(w.r * mm, hole_size(w.r * mm, w.owner), size_mm[t], low_mm, high_mm) for w in ws]
        got_plans = [pl for pl in plans if pl is not None]
        use_o = len(got_plans) == len(ws) and len({pl[0] for pl in got_plans}) == 1 and \
            len({pl[1] for pl in got_plans}) == 1
        if not use_o:
            # SHMESH-49d: no O-grid fits the holes inside the user's edges - a ring in a paved sector put a ring of
            # triangles round every hole (16 a hole on a real base plate): the plate keeps its free-face washers
            no("no O-grid fits its holes inside the user's edges")
            continue
        plan_of: dict[int, tuple[int, int, float, float]] = {}
        if use_o:
            b = max(pl[3] for pl in got_plans) / mm
            R_of = {id(w): pl[2] * w.r for w, pl in zip(ws, got_plans, strict=True)}
            plan_of = {id(w): pl for w, pl in zip(ws, got_plans, strict=True)}
        else:
            b = max(w.R for w in ws) + BOX_FREE * h
            R_of = {id(w): w.R for w in ws}
        beta = math.asin(min(0.99, b / Rb))
        if min(gaps) - 2 * beta < CELL_MIN * h / Rb:
            no("the washers' sectors would touch")
            continue
        # SHMESH-49f: each hole's box is square - across its strip the rings are straight (chords b from the hole's
        # centre), so the box's corners are square and its O-grid diagonals at 45 degrees (an arc of the ring there
        # met the strip's side at 77 degrees: O-grid cells with 38 degree corners, left unmapped); r1 and r2 are the
        # radii the chords end on
        if POLAR_SQUARE:
            r1 = math.sqrt((Rb - b) ** 2 + b * b)
            r2 = math.sqrt((Rb + b) ** 2 + b * b)
        else:
            r1, r2 = Rb - b, Rb + b
        if Ro - r2 < SNAP * h:
            r2 = Ro
        if Ri > 0 and r1 - Ri < SNAP * h:
            r1 = Ri
        if Ri == 0 and r1 < 2 * h:
            no("no room for a core inside the bolt circle")
            continue
        if r2 > Ro or r1 < Ri:
            no("the washers' sectors run past the plate")
            continue
        start = Ri if Ri > 0 and not inner_shared and POLAR_INNER_SPOKES else r1
        # SHMESH-49b: a washer's strip is bounded by two lines parallel to its radial line, b each side, from the
        # bolt circle's inner ring out to the rim: as wide (2b) at the rim as at its box, so its quarter count fits all
        # the way along (radial spokes fanned out: the arcs at the rim came out 1.2 times the box). SHMESH-49d: inside
        # the inner ring (to a central hole) the strip's sides turn radial - parallel sides of neighbouring strips
        # met there in thin wedges, paved with triangles
        if 2 * math.asin(min(0.99, b / r1)) > min(gaps) - CELL_MIN * h / r1:
            no("the washers' strips would touch")
            continue
        if use_o:
            # the strip's count runs on to the rim and the core: sized by its longest arc, checked against the hole
            n0, rows0, ratio0, b0 = next(iter(plan_of.values()))
            # (an arc is a little longer than the box's chord: 5% over our own bound, never past the user's edge)
            seg_hi = 1.05 * BOX_SEG[1] * size_mm[t] if high_mm is None else min(1.05 * BOX_SEG[1] * size_mm[t],
                                                                                  0.98 * high_mm)
            arc = 2 * r1 * math.asin(min(1.0, b / r1))
            q = max(n0 // 4, math.ceil(arc * mm / seg_hi - 1e-9))
            # (and inside the inner ring, where the sides turn radial, the arc at the central hole stays over the
            # user's shortest edge)
            inner_arc = 2 * start * math.asin(min(1.0, b / r1)) * mm / q
            if q > n0 // 4 or inner_arc < float(low_mm or 0.0):
                if min(2 * math.pi * w.r * mm / (4 * q) for w in ws) < float(low_mm or 0.0) or 4 * q > WASHER_MAX \
                        or inner_arc < float(low_mm or 0.0):
                    no("a strip's elements would come out past the user's edges from the hole to the rim")
                    continue
                plan_of = dict.fromkeys(plan_of, (4 * q, rows0, ratio0, b0))
        spokes: list[tuple[float, float]] = []      # (angle, offset): a line parallel to that radial line
        for i, (a, _w) in enumerate(ang):
            spokes += [(a, -b), (a, b)]
            if gaps[i] * Rb > (POLAR_GAP + 2 * b / h) * h:
                spokes.append((a + gaps[i] / 2, 0.0))

        def on(r: float, a: float, off: float, C=C, e1=e1, e2=e2) -> np.ndarray:
            """The point at radius r on the line parallel to angle a's radial line, `off` to its side."""
            u_ = math.cos(a) * e1 + math.sin(a) * e2
            v_ = -math.sin(a) * e1 + math.cos(a) * e2
            return C + math.sqrt(max(r * r - off * off, 0.0)) * u_ + off * v_

        def sector_corners(a: float, r1=r1, r2=r2, b=b, on=on) -> list[np.ndarray]:
            return [on(r, a, sg * b) for r, sg in ((r1, -1), (r2, -1), (r2, 1), (r1, 1))]

        def tools(C=C, Ri=Ri, Ro=Ro, ang=ang, n_face=n_face, r1=r1, r2=r2, spokes=spokes, start=start, on=on,
                  use_o=use_o, R_of=R_of, sector_corners=sector_corners, e1=e1, e2=e2) -> list[tuple[int, int]]:
            out = []
            centre_pt = occ.addPoint(*[float(x) for x in C])
            centre_of = {a_: w_.centre for a_, w_ in ang}

            def bowed(t0: int, t1: int, p0: np.ndarray, p1: np.ndarray, hc: np.ndarray) -> tuple[int, int]:
                """SHMESH-49h: a box side from p0 to p1, bowed away from its hole's centre hc by POLAR_BOW."""
                mid = 0.5 * (p0 + p1)
                away = mid - hc
                away = away - float(away @ n_face) * n_face
                away /= max(float(np.linalg.norm(away)), 1e-300)
                sag = 0.5 * float(np.linalg.norm(p1 - p0)) * math.tan(math.radians(POLAR_BOW) / 2)
                if sag <= 0:
                    return (1, occ.addLine(t0, t1))
                return (1, occ.addCircleArc(t0, occ.addPoint(*[float(x) for x in mid + sag * away]), t1, center=False))
            for r in sorted({r1, r2} - {Ri, Ro, 0.0}):
                # the ring at r: straight across each strip (its box's square side), an arc between strips
                on_r = sorted(((math.atan2(float((on(r, a_, off) - C) @ e2), float((on(r, a_, off) - C) @ e1))
                                % (2 * math.pi), a_, off) for a_, off in spokes))
                tags = [occ.addPoint(*[float(x) for x in on(r, a_, off)]) for _ang, a_, off in on_r]
                for j in range(len(on_r)):
                    k_ = (j + 1) % len(on_r)
                    same_strip = POLAR_SQUARE and on_r[j][1] == on_r[k_][1] and on_r[j][2] < 0 < on_r[k_][2]
                    if same_strip:
                        out.append(bowed(tags[j], tags[k_], on(r, on_r[j][1], on_r[j][2]),
                                         on(r, on_r[k_][1], on_r[k_][2]), centre_of[on_r[j][1]]))
                    else:
                        out.append((1, occ.addCircleArc(tags[j], centre_pt, tags[k_])))
            for a, off in spokes:
                bend = on(r1, a, off)
                if off != 0.0 and POLAR_SQUARE and POLAR_BOW > 0 and r2 < Ro - 1e-9:
                    # a box's side (r1 to r2 along its strip) bowed out, then straight on to the rim
                    bend2 = on(r2, a, off)
                    out.append(bowed(occ.addPoint(*[float(x) for x in bend]), occ.addPoint(*[float(x) for x in bend2]),
                                     bend, bend2, centre_of[a]))
                    if r2 < Ro - 1e-9:
                        out.append((1, occ.addLine(occ.addPoint(*[float(x) for x in bend2]),
                                                   occ.addPoint(*[float(x) for x in on(Ro * 1.02, a, off)]))))
                else:
                    out.append((1, occ.addLine(occ.addPoint(*[float(x) for x in bend]),
                                               occ.addPoint(*[float(x) for x in on(Ro * 1.02, a, off)]))))
                if start < r1 - 1e-9:
                    # inside the inner ring: radial, through the bend, in to the central hole
                    u_ = (bend - C) / max(float(np.linalg.norm(bend - C)), 1e-300)
                    out.append((1, occ.addLine(occ.addPoint(*[float(x) for x in C + 0.98 * start * u_]),
                                               occ.addPoint(*[float(x) for x in bend]))))
            for a, w in ang:
                R = R_of[id(w)]
                if not use_o:
                    out.append((1, occ.addCircle(*[float(x) for x in w.centre], float(R),
                                                 zAxis=[float(x) for x in n_face], xAxis=[float(x) for x in w.xaxis])))
                    continue
                cs = sector_corners(a)
                # SHMESH-49e: the ring's and hole's points toward the strip box's own corners (the box is longer than
                # wide where the ring sits off the bolt circle's middle: points at 45 degrees left cells with 38 degree
                # corners, past what is mapped); the redraw starts the hole at the first of them
                dgs = []
                for cp in cs:
                    d_ = cp - w.centre
                    d_ = d_ - float(d_ @ n_face) * n_face
                    dgs.append(d_ / max(float(np.linalg.norm(d_)), 1e-300))
                w.seam = [float(x) for x in dgs[0]]
                # the ring's seam on a corner's diagonal: no seam inside a quarter
                out.append((1, occ.addCircle(*[float(x) for x in w.centre], float(R),
                                             zAxis=[float(x) for x in n_face], xAxis=[float(x) for x in dgs[0]])))
                for cp, dg in zip(cs, dgs, strict=True):
                    q1 = occ.addPoint(*[float(x) for x in w.centre + R * dg])
                    out.append((1, occ.addLine(occ.addPoint(*[float(x) for x in cp]), q1)))
                    out.append((1, occ.addLine(q1, occ.addPoint(*[float(x) for x in w.centre + 0.5 * w.r * dg]))))
            return out

        # tried on a copy first: cells no smaller than CELL_MIN face sizes, every ring one circle, no shared edge cut
        shared = [c for cl in loops for c in cl if len(gmsh.model.getAdjacencies(1, c)[0]) > 1]
        copy = occ.copy([(2, t)])
        _o, cmap = occ.fragment(copy, tools())
        occ.synchronize()
        cells = [s for d, s in cmap[0] if d == 2]
        cut_curves = {c for got in cmap[1:] for d, c in got if d == 1}
        trial = sorted({abs(int(c)) for s in cells for _d, c in gmsh.model.getBoundary([(2, s)], oriented=False)})
        # (a hole's own arcs aside: its circle's seam may split a quarter - a chain, mapped all the same)
        on_holes = {c for c in trial for w in ws
                    if np.all(np.abs(np.linalg.norm(_samples(gmsh, c, 4) - w.centre, axis=1) - w.r) <= CIRCLE_TOL * w.r)}
        bad = any(float(occ.getMass(1, c)) < CELL_MIN * h for c in trial if c not in on_holes)
        for _a, w in ang:
            R = R_of[id(w)]
            on_ring = [c for c in trial if np.all(np.abs(np.linalg.norm(_samples(gmsh, c, 4) - w.centre, axis=1) - R)
                                                  <= 1e-4 * R)]
            bad = bad or len(on_ring) != (4 if use_o else 1)
        if shared:
            from scipy.spatial import cKDTree

            for c in shared:
                tree = cKDTree(_samples(gmsh, c, 256))
                full = float(occ.getMass(1, c))
                for x in trial:
                    if tree.query(_samples(gmsh, x, 4))[0].max() <= 1e-3 * h + full / 256 and \
                            float(occ.getMass(1, x)) < (1 - 1e-6) * full:
                        bad = True
        occ.remove([(2, s) for s in cells], recursive=True)
        occ.synchronize()
        _remove_loose(gmsh, cut_curves)
        if bad:
            no("a sector would come out too small, a ring or a shared edge cut")
            continue
        edge_pts = np.vstack([_samples(gmsh, c, 64) for cl in loops for c in cl])
        _o, omap = occ.fragment([(2, t)], tools())
        occ.synchronize()
        k = tag_of.pop(t)
        if made_out is not None:
            made_out.add(k)
        new = [s for d, s in omap[0] if d == 2]
        for s in new:
            tag_of[s] = k
        if cells_out is not None:
            cells_out.update(new)
        _remove_loose(gmsh, {c for got in omap[1:] for d, c in got if d == 1})
        curves = sorted({abs(int(c)) for s in new for _d, c in gmsh.model.getBoundary([(2, s)], oriented=False)})
        if bands_out is not None and POLAR_BAND and Ri > 0 and not inner_shared and start >= r1 - 1e-9:
            # SHMESH-49l: the band round the central opening, meshed in rings (band_mesh) once its ring is counted
            for s_ in new:
                bl = [abs(int(c)) for _d, c in gmsh.model.getBoundary([(2, s_)], oriented=False)]
                on_in = [c for c in bl if np.all(np.abs(np.linalg.norm(_samples(gmsh, c, 4) - C, axis=1) - Ri)
                                                  <= 1e-4 * Ri)]
                if on_in and len(_curve_loops(gmsh, s_)[1]) == 2:
                    bands_out.append(Band(face=s_, owner=k, centre=C, normal=n_face, inner=on_in,
                                          outer=[c for c in bl if c not in on_in], r_in=Ri))
        on_circle: set[int] = set()
        for _a, w in ang:
            R = R_of[id(w)]
            tol = 1e-4 * R
            ring_c, hole_c = [], []
            for c in curves:
                d = np.linalg.norm(_samples(gmsh, c, 4) - w.centre, axis=1)
                if np.all(np.abs(d - w.r) <= CIRCLE_TOL * w.r):
                    hole_c.append(c)
                elif np.all(np.abs(d - R) <= tol):
                    ring_c.append(c)
                elif use_o and d.min() >= w.r - tol and d.max() <= R + tol and abs(d[0] - d[-1]) > 0.5 * (R - w.r):
                    preset[c] = plan_of[id(w)][1]                    # a row line from the hole to the ring
            on_circle |= set(ring_c) | set(hole_c)
            if use_o:
                pl = plan_of[id(w)]
                for c in ring_c:
                    preset[c] = pl[0] // 4
                holes.update(hole_c)
                w.n, w.rows, w.R = pl[0], pl[1], R
                w.out_mm = 2 * math.pi * R * mm / pl[0]
                w.info["kind"] = "o-grid in a sector of a round plate"
                ogrid.append(w)
                continue
            w.ring, w.hole_after = ring_c[0], hole_c
            for s in new:
                _l3, lp = _curve_loops(gmsh, s)
                lp = [sorted(abs(int(c)) for c in cl) for cl in lp]
                if len(lp) == 2 and [w.ring] in lp and _on_hole(gmsh, w, [c for cl in lp for c in cl if c != w.ring]):
                    w.annulus = s
            if not w.annulus:
                raise ValueError("a washer's ring did not close round its hole")
            w.info["kind"] = "ring in a sector of a round plate"
            made.append(w)
        if cuts is not None:
            from scipy.spatial import cKDTree

            tree = cKDTree(edge_pts)
            for c in curves:
                if c not in on_circle and tree.query(_samples(gmsh, c, 2)[1])[0] > 1e-3 * h:
                    cuts.add(c)
    return made, why, ogrid, preset, holes


__all__ = ["WASHERS", "MAPPED", "Washer", "plan_washers", "imprint", "set_counts", "ring_mesh", "map_faces", "block_faces", "reseat_holes",
           "polar_faces"]

