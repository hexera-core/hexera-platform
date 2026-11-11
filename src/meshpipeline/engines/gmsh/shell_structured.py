# Responsibility: The STRUCTURED steps of the shell mesh - what makes it look like an engineer's hand-made mesh:
# WASHERS round holes (SHMESH-38: a concentric ring of quads hugging each round hole of a flat face, an even number of
# elements round it, the field mesh growing out from the ring) and MAPPED quads on faces with four natural corners
# (SHMESH-39: a transfinite grid, opposite sides counted alike, pipes as a grid of rows along the axis and columns
# round it). Everything here is planned from the face's own geometry and the sizes the plan already chose.
# Boundaries: works on the gmsh model the shell adapter built (engines/gmsh/shell_adapter.py), in that process;
# reads and writes nothing else. Deterministic: every choice is made in a fixed order with fixed numbers.
# Collaborates with: engines/gmsh/shell_adapter.py (calls it before and after the 2D mesh), engines/shell_structure.py
# (measures the result).
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

#: SHMESH-38: washers round the round holes of flat faces
WASHERS = True
#: a hole gets a washer when it is at most WASHER_ACROSS times its face's element size across (a bigger hole is a
#: cut-out: the field mesh already follows it)
WASHER_ACROSS = 3.0
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
#: SHMESH-39: faces with four natural corners meshed as mapped grids
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
    ring: int = 0                   # after the imprint: the ring's circle
    annulus: int = 0                # ... the surface between hole and ring
    hole_after: list[int] = field(default_factory=list)


def _samples(gmsh, curve: int, n: int = 24) -> np.ndarray:
    lo, hi = gmsh.model.getParametrizationBounds(1, curve)
    ts = np.linspace(lo[0], hi[0], n + 1)
    return np.asarray(gmsh.model.getValue(1, curve, ts.tolist()), dtype=float).reshape(-1, 3)


def _circle_of(gmsh, curves: list[int]) -> tuple[np.ndarray, float, np.ndarray, np.ndarray] | None:
    """(centre, radius, normal, first point) when the loop's curves together are one circle, else None."""
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


def _count(r_mm: float, hole_mm: float, face_mm: float) -> tuple[int, int, float] | None:
    """SHMESH-38: (elements round, rows, ring radius over the hole's) for a hole of radius r_mm whose plan puts
    elements of hole_mm round it in a face of face_mm elements: the fewest elements round (a multiple of WASHER_STEP,
    at least WASHER_MIN and the plan's own count) with near-square rows whose outer edge (WASHER_RING times the hole)
    has elements no longer than the face's; None when no count up to WASHER_MAX does it."""
    own = math.ceil(2 * math.pi * r_mm / max(hole_mm, 1e-12) - 1e-9)
    n0 = max(WASHER_MIN, WASHER_STEP * math.ceil(own / WASHER_STEP))
    for n in range(n0, WASHER_MAX + 1, WASHER_STEP):
        q = math.exp(2 * math.pi / n)
        for rows in range(1, WASHER_ROWS + 1):
            ratio = q ** rows
            if ratio < WASHER_RING[0]:
                continue
            ratio = min(ratio, WASHER_RING[1])
            if 2 * math.pi * r_mm * ratio / n <= face_mm * (1 + 1e-9):
                return n, rows, ratio
            break
    return None


def plan_washers(gmsh, tag_of: dict[int, int], size_mm: dict[int, float], hole_size, mm: float
                 ) -> tuple[list[Washer], dict[str, int]]:
    """SHMESH-38: the washers to make: every inner loop of a flat face that is one round hole (a circle no other face
    shares), at most WASHER_ACROSS of the face's element size across. `size_mm`: each surface's element size;
    `hole_size(r_mm, owner)`: the element length the plan puts round a hole of that radius. Returns (the washers,
    {why a hole has none: count})."""
    skipped: dict[str, int] = {}
    found: list[Washer] = []
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
        for j, cl in enumerate(loops):
            if j == outer:
                continue
            if any(len(gmsh.model.getAdjacencies(1, c)[0]) != 1 for c in cl):
                skipped["shared with another face"] = skipped.get("shared with another face", 0) + 1
                continue
            got = _circle_of(gmsh, cl)
            if got is None:
                skipped["not round"] = skipped.get("not round", 0) + 1
                continue
            centre, r, n, first = got
            if 2 * r * mm > WASHER_ACROSS * size_mm[t]:
                skipped["larger than the washer limit"] = skipped.get("larger than the washer limit", 0) + 1
                continue
            plan = _count(r * mm, hole_size(r * mm, k), size_mm[t])
            if plan is None:
                skipped["no count fits"] = skipped.get("no count fits", 0) + 1
                continue
            cnt, rows, ratio = plan
            xa = first - centre
            xa = xa - float(xa @ n) * n
            xa /= max(float(np.linalg.norm(xa)), 1e-300)
            found.append(Washer(face=t, owner=k, hole=cl, centre=centre, normal=n, xaxis=xa, r=r, R=ratio * r,
                                n=cnt, rows=rows, out_mm=2 * math.pi * ratio * r * mm / cnt))
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
                skipped["too close to an edge or another hole"] = skipped.get("too close to an edge or another hole", 0) + 1
                continue
            w.R, w.shrunk = limit, True
            w.rows = max(1, min(w.rows, round(math.log(w.R / w.r) / (2 * math.pi / w.n))))
            w.out_mm = 2 * math.pi * w.R * mm / w.n
        keep.append(w)
    return keep, skipped


def imprint(gmsh, washers: list[Washer], tag_of: dict[int, int]) -> None:
    """SHMESH-38: each washer's ring drawn on its face (a circle about the hole's centre, starting where the hole's
    circle starts), the face cut along it; `tag_of` follows (the new surfaces keep the face's sewn face). Edges the face
    shares keep their tags (the fragment changes only what it cuts), so every junction stays conformal."""
    by_face: dict[int, list[Washer]] = {}
    for w in washers:
        by_face.setdefault(w.face, []).append(w)
    for t, ws in sorted(by_face.items()):
        tools = []
        for w in ws:
            c = gmsh.model.occ.addCircle(*[float(x) for x in w.centre], float(w.R), zAxis=[float(x) for x in w.normal],
                                         xAxis=[float(x) for x in w.xaxis])
            tools.append((1, c))
        _out, omap = gmsh.model.occ.fragment([(2, t)], tools)
        gmsh.model.occ.synchronize()
        k = tag_of.pop(t)
        new = [s for d, s in omap[0] if d == 2]
        for s in new:
            tag_of[s] = k
        for w, got in zip(ws, omap[1:], strict=True):
            curves = [c for d, c in got if d == 1]
            if len(curves) != 1:
                raise ValueError("a washer's ring did not come out as one circle")
            w.ring = curves[0]
            for s in new:
                _l, loops = gmsh.model.occ.getCurveLoops(s)
                loops = [[abs(int(c)) for c in cl] for cl in loops]
                if len(loops) == 2 and [w.ring] in loops:
                    w.annulus = s
                    w.hole_after = [cl for cl in loops if cl != [w.ring]][0]
            if not w.annulus:
                raise ValueError("a washer's ring did not close round its hole")


def set_counts(gmsh, washers: list[Washer]) -> set[int]:
    """SHMESH-38: the hole's circle and the ring's carry the washer's count, evenly spaced; returns those curves."""
    fixed: set[int] = set()
    for w in washers:
        lengths = [float(gmsh.model.occ.getMass(1, c)) for c in w.hole_after]
        total = sum(lengths)
        counts = [max(1, round(w.n * x / total)) for x in lengths]
        counts[int(np.argmax(lengths))] += w.n - sum(counts)
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


def ring_mesh(gmsh, w: Washer, index: np.ndarray, xyz: np.ndarray, mm: float) -> tuple[np.ndarray, np.ndarray]:
    """SHMESH-38: the washer's own structured mesh, in place of whatever gmsh put between hole and ring: each node of the
    hole's circle joined to the ring's node at its angle, the rows between them on circles whose radii grow evenly
    (in ratio), so every quad is near square. Returns (the new nodes, mm; the quads, node indices - new nodes numbered
    from len(xyz))."""
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
    if float(np.abs(gap).max()) > 0.45 * step:
        raise ValueError("a washer's hole and ring nodes do not face each other")
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


#: SHMESH-39: a face is mapped when it has four sides (one loop of four curves), each corner between MAP_CORNER degrees,
#: and its opposite sides alike within MAP_ALIKE times (a grid stretched further leans or tapers)
MAP_CORNER = (40.0, 140.0)
MAP_ALIKE = 2.0
#: ... a curve's count is its class's mean length over the finest size of the faces it bounds
#: (a count of 1 stays 1: one element across a strip)


def _ends(gmsh, c: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(start point, end point, direction leaving the start, direction leaving the end) of a curve."""
    lo, hi = gmsh.model.getParametrizationBounds(1, c)
    a, b = float(lo[0]), float(hi[0])
    pa = np.asarray(gmsh.model.getValue(1, c, [a]), dtype=float)
    pb = np.asarray(gmsh.model.getValue(1, c, [b]), dtype=float)
    da = np.asarray(gmsh.model.getDerivative(1, c, [a]), dtype=float)
    db = -np.asarray(gmsh.model.getDerivative(1, c, [b]), dtype=float)
    return pa, pb, da, db


def _four_sided(gmsh, t: int) -> tuple[list[int], list[int]] | None:
    """(its four curves in loop order, its four corner points) when the surface is one loop of four distinct curves
    meeting at corners inside MAP_CORNER, opposite sides alike within MAP_ALIKE; else None."""
    _l, loops = gmsh.model.occ.getCurveLoops(t)
    if len(loops) != 1:
        return None
    cl = [abs(int(c)) for c in loops[0]]
    if len(cl) != 4 or len(set(cl)) != 4:
        return None
    ends = {c: _ends(gmsh, c) for c in cl}
    pts = {c: [abs(int(p)) for _d, p in gmsh.model.getBoundary([(1, c)], oriented=True)] for c in cl}
    if any(len(v) != 2 or v[0] == v[1] for v in pts.values()):
        return None
    corners: list[int] = []
    for j in range(4):
        a, b = cl[j], cl[(j + 1) % 4]
        shared = set(pts[a]) & set(pts[b])
        if len(shared) != 1:
            return None
        p = shared.pop()
        da = ends[a][2] if pts[a][0] == p else ends[a][3]
        db = ends[b][2] if pts[b][0] == p else ends[b][3]
        cos = float(da @ db) / max(float(np.linalg.norm(da) * np.linalg.norm(db)), 1e-300)
        ang = math.degrees(math.acos(max(-1.0, min(1.0, cos))))
        if not (MAP_CORNER[0] <= ang <= MAP_CORNER[1]):
            return None
        corners.append(p)
    length = [float(gmsh.model.occ.getMass(1, c)) for c in cl]
    for i in (0, 1):
        x, y = length[i], length[i + 2]
        if max(x, y) > MAP_ALIKE * max(min(x, y), 1e-300):
            return None
    return cl, corners


def map_faces(gmsh, tag_of: dict[int, int], size_mm: dict[int, float], mm: float, *, skip: set[int],
              preset: dict[int, int], low_mm: float | None = None, fine_at: dict[int, float] | None = None,
              growth: float = 1.3) -> tuple[dict[int, list[int]], dict[int, int]]:
    """SHMESH-39: every four-sided face (_four_sided) not in `skip` is meshed as a mapped grid: its opposite sides take one
    count, and so does every curve they share with other mapped faces (a pipe's pieces, a run of plates: one class
    of curves, one count). A class's count is its curves' mean length over the finest size of the faces it bounds
    (never so many that a segment comes out under `low_mm`, the user's shortest edge), made even when the class also
    bounds a free face (Blossom's condition there); a class holding a curve already counted (`preset`: a mapped
    strip's, a washer's) takes that count, and two different presets in one class leave its faces free. A face with a
    smaller piece of the CAD at a corner (`fine_at`: the size graded to there, mm, more than one growth step finer)
    stays free (a mapped grid cannot grade). Returns ({mapped face: its corners}, {curve: count})."""
    cand: dict[int, tuple[list[int], list[int]]] = {}
    for t in sorted(tag_of):
        if t in skip:
            continue
        got = _four_sided(gmsh, t)
        if got is None:
            continue
        if fine_at and min((fine_at.get(p, math.inf) for p in got[1]), default=math.inf) * growth < size_mm[t]:
            continue
        cand[t] = got
    length = {c: float(gmsh.model.occ.getMass(1, c)) for t in cand for c in cand[t][0]}
    while True:
        parent: dict[int, int] = {}

        def root(c: int) -> int:
            parent.setdefault(c, c)
            while parent[c] != c:
                parent[c] = parent[parent[c]]
                c = parent[c]
            return c

        for t, (cl, _p) in sorted(cand.items()):
            for i in (0, 1):
                a, b = root(cl[i]), root(cl[i + 2])
                if a != b:
                    parent[max(a, b)] = min(a, b)
        members: dict[int, list[int]] = {}
        for c in sorted(length):
            if any(c in cand[t][0] for t in cand):
                members.setdefault(root(c), []).append(c)
        faces_of: dict[int, set[int]] = {}
        for t, (cl, _p) in cand.items():
            for c in cl:
                faces_of.setdefault(root(c), set()).add(t)
        bad: set[int] = set()
        counts: dict[int, int] = {}
        for r, cs in sorted(members.items()):
            pre = {preset[c] for c in cs if c in preset}
            if len(pre) > 1:
                bad |= faces_of[r]
                continue
            if pre:
                n = pre.pop()
            else:
                h = min(size_mm[t] for t in faces_of[r]) / mm
                n = max(1, round(float(np.mean([length[c] for c in cs])) / h))
                if low_mm is not None:
                    n = max(1, min(n, math.floor(min(length[c] for c in cs) * mm / low_mm + 1e-9)))
                free_beside = any(s not in cand for c in cs for s in gmsh.model.getAdjacencies(1, c)[0] if s in tag_of)
                if n > 1 and n % 2 and free_beside and (low_mm is None or min(length[c] for c in cs) * mm / (n + 1) >= low_mm):
                    n += 1
            for c in cs:
                counts[c] = n
        if not bad:
            break
        for t in bad:
            cand.pop(t)
    for c, n in sorted(counts.items()):
        gmsh.model.mesh.setTransfiniteCurve(c, n + 1)
    out: dict[int, list[int]] = {}
    for t, (_cl, corners) in sorted(cand.items()):
        gmsh.model.mesh.setTransfiniteSurface(t, cornerTags=corners)
        gmsh.model.mesh.setRecombine(2, t)
        out[t] = corners
    return out, counts


__all__ = ["WASHERS", "MAPPED", "Washer", "plan_washers", "imprint", "set_counts", "ring_mesh", "map_faces"]
