# Responsibility: THE INSIDE-SPACE MAP of one part. The space around and inside the part's surface is
# flooded on a grid, deterministically, to find the pockets of space inside the part (a bore, a
# channel, a cavity), the MOUTHS where a pocket meets the open air outside, and the deepest point of
# each pocket. One map per part drives the openings, the flow kind, the seed, the caps and the leak
# test, so those steps can never disagree (docs/RULES.md, INS).
# Owns: the grid, the wall's rasterisation, the flood, what a pocket and a mouth are, the deepest
# point, and the flood leak test of a staged fluid boundary.
# Boundaries: numpy and scipy.ndimage over triangles, in the triangles' own units. No OpenCASCADE,
# no engine, no storage, no model, nothing about what the part is and never a file name: every
# decision is read off the grid.
"""WHAT THE MAP IS.

The part's surface is drawn into a grid of cubes (the WALL: every cube a triangle passes through).
Every closed loop of the surface that could be the edge of an opening - an open rim, or the outline
of a smooth patch such as a bore's mouth at a cut end face - gets a lid laid across it (its CAP),
and the free cubes are flooded from the grid's boundary. Space the flood cannot reach is either

  - inside a closed shell (the metal of a solid part, or a body that IS the fluid), the ENCLOSED
    space, which the flood could not reach with no cap at all; or
  - a POCKET: space the open air reaches only through caps.

A cap the open air touches on one side and a pocket on the other is a MOUTH of that pocket. A cap
with a pocket on both sides (a step inside a bore, the outline of a groove in its wall) splits one
pocket in two and is no mouth: it is taken away and the space flooded again, until every cap left
is a mouth or touches nothing. Of two mouth caps that close one pocket at one place (a bore's
mouth and the outline of the end face around it) the tighter is the mouth.

So an opening is never a guess about one loop: it is a place where the inside of the part meets the
outside. A loop too small for the grid to see (fewer than MIN_ACROSS cubes across) is not decided by
the map; the caller keeps the hole finder's reading of it (cad/open_ends), so no small opening is
ever dropped.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

#: The grid's cube count for one map (a few seconds, ~0.5 GB at most): the cube edge is the part's
#: box volume over this, cube-rooted, never finer than MIN_CELL of the part's diagonal.
GRID_CELLS = 6_000_000
#: ...and never more than this many cubes along one side (a long thin part).
MAX_SIDE = 1024
MIN_CELL = 2e-4
#: Empty cubes kept round the part's box, so the open air surrounds it on every side.
MARGIN = 3
#: A loop is mapped only when it is at least this many cubes across; a smaller one is the hole
#: finder's to read (cad/open_ends), and is never dropped for being small.
MIN_ACROSS = 4.0
#: At most this many loops are capped (the largest first): a perforated plate has thousands.
MAX_LOOPS = 400
#: A pocket smaller than this many cubes is a crevice of the grid, not space inside the part.
MIN_POCKET_CELLS = 8
#: Rounds of taking away the caps that split a pocket.
MAX_ROUNDS = 24
#: A loop strays from its plane by more than this share of its size: its cap is the lid builder's
#: own surface (cad/lids), not the flat disc of its plane.
CURVED_LOOP = 0.03
#: A pocket counts as a passage of the part when it holds at least this share of the largest
#: pocket's space; a smaller one (a bolt hole beside a flange's bore, a blind dimple) is a side
#: pocket: its mouths are kept, proposed as not an opening.
PASSAGE_SHARE = 0.05
#: Points of a cap read for what lies on either side of it (evenly spread, deterministic).
MAX_PROBES = 4000
#: A pocket's lining is the surface piece that holds at least this share of the surface round it.
LINING_SHARE = 0.25

_SIX = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))


class PartialMap(ValueError):
    """The part has more loops than the map caps: no map is better than a partial one."""


@dataclass
class Mouth:
    """Where a pocket meets the outside: the surface loop its cap is fitted to."""
    id: int
    kind: str                       # "rim" (an open edge) | "bore" (a thick wall's cut)
    vidx: np.ndarray                # the loop's points, by index into the surface
    eidx: np.ndarray                # the loop's edges
    centroid: np.ndarray
    normal: np.ndarray              # out of the pocket: the way the fluid leaves by it
    area: float
    wh: tuple[float, float]
    planarity: float
    pockets: tuple[int, ...] = ()   # the pockets it opens
    cells: int = 0                  # the cap's cubes

    @property
    def diameter(self) -> float:
        return 2.0 * math.sqrt(max(self.area, 0.0) / math.pi)


@dataclass
class Pocket:
    id: int
    cells: int
    volume: float
    deepest: np.ndarray             # the point farthest from every wall and cap
    clearance: float                # how far that point is from the nearest wall, about
    mouths: list[int] = field(default_factory=list)
    share: float = 1.0              # its space over the largest pocket's

    @property
    def passage(self) -> bool:
        return bool(self.mouths) and self.share >= PASSAGE_SHARE


@dataclass
class InsideMap:
    h: float
    origin: np.ndarray
    shape: tuple[int, int, int]
    pockets: list[Pocket]
    mouths: list[Mouth]
    enclosed_cells: int             # space inside closed shells (no cap needed to close it)
    loops_capped: int
    loops_small: int                # loops too small for the grid: the hole finder's to read
    notes: list[str] = field(default_factory=list)
    #: the deepest point inside the closed shells (a body that is the fluid: its seed), or None
    enclosed_deepest: np.ndarray | None = None
    enclosed_clearance: float = 0.0
    #: the flood's labels (flat, grid order), the open air's labels, and each pocket's labels: what
    #: `where` reads a point against
    labels: np.ndarray | None = field(default=None, repr=False)
    outside_labels: frozenset = frozenset()
    pocket_labels: dict = field(default_factory=dict)
    #: the cubes inside a closed shell (flat, grid order)
    shell: np.ndarray | None = field(default=None, repr=False)

    def where(self, points) -> list[str]:
        """What each point is in, by the map: "out" (the open air), "pocket" (a pocket inside the
        part), "shell" (inside a closed shell: the part's material, or a body that is the fluid), or
        "" (a wall, a cap, or off the grid)."""
        P = np.atleast_2d(np.asarray(points, dtype=float))
        if self.labels is None:
            return [""] * len(P)
        shape = np.asarray(self.shape)
        ijk = np.floor((P - self.origin) / self.h).astype(np.int64)
        inside = np.all((ijk >= 0) & (ijk < shape), axis=1)
        flat = np.zeros(len(P), dtype=np.int64)
        flat[inside] = (ijk[inside, 0] * shape[1] + ijk[inside, 1]) * shape[2] + ijk[inside, 2]
        lab = np.where(inside, self.labels[flat], 0)
        shell = np.where(inside, self.shell[flat], False) if self.shell is not None else np.zeros(len(P), dtype=bool)
        pocket = {g for gs in self.pocket_labels.values() for g in gs}
        return ["shell" if s else "out" if g in self.outside_labels else "pocket" if g in pocket else ""
                for g, s in zip(lab.tolist(), shell.tolist())]

    def faces_inside_material(self, mesh) -> np.ndarray:
        """The faces of a surface with a closed shell's inside on both sides: a surface that passes
        through itself (two shells overlapping, never joined into one solid) - CAD to be repaired,
        not a fluid boundary to be read."""
        if self.shell is None:
            return np.zeros(len(mesh.faces), dtype=bool)
        both = []
        for sgn in (1.0, -1.0):
            got = np.ones(len(mesh.faces), dtype=bool)
            for step in (1.5, 3.0):
                w = self.where(mesh.centre + sgn * step * self.h * mesh.normal)
                got &= np.asarray([x == "shell" for x in w])
            both.append(got)
        return both[0] & both[1]

    @property
    def enclosed_volume(self) -> float:
        return self.enclosed_cells * self.h ** 3

    def passages(self) -> list[Pocket]:
        return [p for p in self.pockets if p.passage]

    def has_inside(self) -> bool:
        """Space inside the part the fluid can flow through: a pocket with two mouths or more."""
        return any(len(p.mouths) >= 2 for p in self.passages())

    def mouth(self, mid: int) -> Mouth:
        return next(m for m in self.mouths if m.id == mid)

    def passage_of(self, mid: int) -> Pocket | None:
        """The passage a mouth opens (the largest, when it opens several)."""
        m = self.mouth(mid)
        ps = [self.pockets[p] for p in m.pockets if self.pockets[p].passage]
        return max(ps, key=lambda p: p.cells) if ps else None

    def facts(self, mm_per_unit: float = 1000.0) -> dict:
        """What the check stores and the stage reads: whether the part has an inside the fluid can
        flow through, and how the map read it (INS-1)."""
        r = lambda v: [round(float(x) * mm_per_unit, 2) for x in v]  # noqa: E731
        passages = self.passages()
        out = {"rule": "INS-1", "has_inside": self.has_inside(), "passages": len(passages),
               "mouths_per_passage": [len(p.mouths) for p in passages],
               "side_pockets": len([p for p in self.pockets if not p.passage]),
               "cell_mm": round(self.h * mm_per_unit, 4),
               "enclosed_mm3": round(self.enclosed_volume * mm_per_unit ** 3, 3)}
        if passages:
            out["deepest_mm"] = r(passages[0].deepest)
            out["clearance_mm"] = round(passages[0].clearance * mm_per_unit, 3)
        elif self.enclosed_deepest is not None:
            out["deepest_mm"] = r(self.enclosed_deepest)
            out["clearance_mm"] = round(self.enclosed_clearance * mm_per_unit, 3)
        return out

    def as_dict(self, mm_per_unit: float = 1000.0) -> dict:
        r = lambda v: [round(float(x) * mm_per_unit, 3) for x in v]  # noqa: E731
        return {
            "cell_mm": round(self.h * mm_per_unit, 4), "grid": list(self.shape),
            "pockets": [{"id": p.id, "volume_mm3": round(p.volume * mm_per_unit ** 3, 3),
                         "share": round(p.share, 4), "passage": p.passage, "mouths": list(p.mouths),
                         "deepest_mm": r(p.deepest), "clearance_mm": round(p.clearance * mm_per_unit, 3)}
                        for p in self.pockets],
            "mouths": [{"id": m.id, "kind": m.kind, "centroid_mm": r(m.centroid),
                        "normal": [round(float(x), 4) for x in m.normal],
                        "diameter_mm": round(m.diameter * mm_per_unit, 3), "pockets": list(m.pockets)}
                       for m in self.mouths],
            "enclosed_mm3": round(self.enclosed_volume * mm_per_unit ** 3, 3),
            "loops_capped": self.loops_capped, "loops_small": self.loops_small,
            "has_inside": self.has_inside(), "notes": list(self.notes)}


# ------------------------------------------------------------------------------ the grid ----
@dataclass
class Grid:
    origin: np.ndarray
    h: float
    shape: tuple[int, int, int]

    @classmethod
    def around(cls, lo, hi, cells: int = GRID_CELLS, h_max: float | None = None) -> Grid:
        lo, hi = np.asarray(lo, dtype=float), np.asarray(hi, dtype=float)
        size = np.maximum(hi - lo, 0.0)
        diag = float(np.linalg.norm(size)) or 1.0
        h = max(float(size.max()) / max(cells, 1) ** (1.0 / 3.0), MIN_CELL * diag)
        for _ in range(8):            # the margin's cubes count too: settle h with them in
            vol = float(np.prod(size + 2.0 * MARGIN * h + h))
            h = max((vol / cells) ** (1.0 / 3.0), float(size.max() + 2 * MARGIN * h) / MAX_SIDE,
                    MIN_CELL * diag)
        if h_max is not None:
            h = min(h, float(h_max))
        origin = lo - MARGIN * h
        shape = tuple(int(math.ceil(float(s) / h)) + 2 * MARGIN + 1 for s in size)
        return cls(origin=origin, h=h, shape=shape)  # type: ignore[arg-type]

    @property
    def n(self) -> int:
        return int(np.prod(self.shape))

    def index(self, p) -> tuple[int, int, int]:
        q = np.floor((np.asarray(p, dtype=float) - self.origin) / self.h).astype(int)
        q = np.clip(q, 0, np.asarray(self.shape) - 1)
        return int(q[0]), int(q[1]), int(q[2])

    def centre(self, ijk) -> np.ndarray:
        return self.origin + (np.asarray(ijk, dtype=float) + 0.5) * self.h


def raster(T: np.ndarray, grid: Grid) -> np.ndarray:
    """The cubes the triangles T (n, 3, 3) pass through, as a flat index array (sorted, unique).
    Conservative: every cube whose box a triangle's plane crosses inside the triangle's reach is
    marked, so no path from cube face to cube face crosses the surface unmarked (the flood cannot
    leak through a wall)."""
    T = np.asarray(T, dtype=np.float64).reshape(-1, 3, 3)
    if len(T) == 0:
        return np.zeros(0, dtype=np.int64)
    G = (T - grid.origin) / grid.h
    n = np.cross(G[:, 1] - G[:, 0], G[:, 2] - G[:, 0])
    nl = np.linalg.norm(n, axis=1)
    shape = np.asarray(grid.shape)
    marked = np.zeros(grid.n, dtype=bool)          # every cube marked once, whatever the order
    # degenerate triangles mark their corners only
    flat_ = nl <= 1e-12
    if flat_.any():
        q = np.floor(G[flat_].reshape(-1, 3)).astype(np.int64)
        marked[_flat(q, shape)] = True
    G, unit = G[~flat_], n[~flat_] / nl[~flat_][:, None]
    # A LARGE TRIANGLE (a CAD face's long fan triangle) is marked column by column over its own
    # plane: every column of cubes its projection on its dominant axis overlaps, through the span
    # of its plane over that column - a superset of the cubes it passes through, at a cost that
    # grows with its area, where splitting a long sliver would make millions of pieces
    e0 = np.stack([G[:, 1] - G[:, 0], G[:, 2] - G[:, 1], G[:, 0] - G[:, 2]], axis=1)
    big = np.linalg.norm(e0, axis=2).max(axis=1) > BIG_TRIANGLE
    for t in np.flatnonzero(big).tolist():
        _columns(G[t], unit[t], marked, shape)
    G, unit = G[~big], unit[~big]
    offs = np.array([[a, b, c] for a in (0, 1) for b in (0, 1) for c in (0, 1)], dtype=np.int64)
    # every triangle split at the middle of its longest side until no side is longer than 0.9 of a
    # cube: then each spans at most two cubes a side and its eight candidates cover it. The work
    # grows with the surface's area in cubes, never with a sliver's length squared.
    todo = [(G, unit)]
    while todo:
        if len(todo) > 1 and sum(len(x[0]) for x in todo) <= 4_000_000:
            # one batch per round of splitting, not one per piece split off
            todo = [(np.concatenate([x[0] for x in todo]), np.concatenate([x[1] for x in todo]))]
        Gs, Us = todo.pop()
        e = np.stack([Gs[:, 1] - Gs[:, 0], Gs[:, 2] - Gs[:, 1], Gs[:, 0] - Gs[:, 2]], axis=1)
        L = np.linalg.norm(e, axis=2)
        long_ = L.max(axis=1) > 0.9
        done = ~long_
        if done.any():
            sub, nn = Gs[done], Us[done]
            mn = np.floor(sub.min(axis=1)).astype(np.int64)
            mx = np.floor(sub.max(axis=1)).astype(np.int64)
            p0 = sub[:, 0]
            reach = 0.5 * np.abs(nn).sum(axis=1)
            for off in offs:
                cand = mn + off
                inside = np.all(cand <= mx, axis=1)
                d = np.abs(np.einsum("tc,tc->t", cand + 0.5 - p0, nn))
                hit = inside & (d <= reach + 1e-9)
                if hit.any():
                    marked[_flat(cand[hit], shape)] = True
        if long_.any():
            Gl, Ul = Gs[long_], Us[long_]
            k = np.argmax(L[long_], axis=1)                 # the longest side: corner k to k+1
            a = Gl[np.arange(len(Gl)), k]
            b = Gl[np.arange(len(Gl)), (k + 1) % 3]
            c = Gl[np.arange(len(Gl)), (k + 2) % 3]
            m = 0.5 * (a + b)
            halves = np.concatenate([np.stack([a, m, c], axis=1), np.stack([m, b, c], axis=1)])
            un = np.concatenate([Ul, Ul])
            for s0 in range(0, len(halves), 2_000_000):
                todo.append((halves[s0:s0 + 2_000_000], un[s0:s0 + 2_000_000]))
    return np.flatnonzero(marked)


#: A triangle with a side longer than this many cubes is marked by columns, not split (raster).
BIG_TRIANGLE = 6.0


def _columns(P3: np.ndarray, n: np.ndarray, marked: np.ndarray, shape: np.ndarray) -> None:
    """Mark the cubes of one triangle (corners in cube units) by columns along its dominant axis:
    the columns whose square its projection overlaps (edge tests with each square's half extent),
    and in each, the cubes its plane spans over the square, held to the triangle's own extent."""
    k = int(np.argmax(np.abs(n)))
    i, j = [a for a in range(3) if a != k]
    P = P3[:, [i, j]]
    lo = np.floor(P.min(axis=0)).astype(np.int64)
    hi = np.floor(P.max(axis=0)).astype(np.int64)
    ci = np.arange(lo[0], hi[0] + 1)
    cj = np.arange(lo[1], hi[1] + 1)
    X, Y = np.meshgrid(ci + 0.5, cj + 0.5, indexing="ij")
    X, Y = X.ravel(), Y.ravel()
    area2 = (P[1, 0] - P[0, 0]) * (P[2, 1] - P[0, 1]) - (P[1, 1] - P[0, 1]) * (P[2, 0] - P[0, 0])
    sgn = 1.0 if area2 >= 0 else -1.0
    ok = np.ones(len(X), dtype=bool)
    for a, b in ((0, 1), (1, 2), (2, 0)):
        ex, ey = P[b, 0] - P[a, 0], P[b, 1] - P[a, 1]
        nx, ny = -ey * sgn, ex * sgn                         # into the triangle
        ok &= nx * (X - P[a, 0]) + ny * (Y - P[a, 1]) + 0.5 * (abs(nx) + abs(ny)) >= -1e-9
    if not ok.any():
        return
    X, Y = X[ok], Y[ok]
    d = float(n @ P3[0])
    kc = (d - n[i] * X - n[j] * Y) / n[k]
    half = 0.5 * (abs(n[i]) + abs(n[j])) / abs(n[k])
    k0 = np.maximum(kc - half, float(P3[:, k].min()))
    k1 = np.minimum(kc + half, float(P3[:, k].max()))
    f0, f1 = np.floor(k0).astype(np.int64), np.floor(k1).astype(np.int64)
    span = int((f1 - f0).max()) if len(f0) else -1
    ii, jj = np.floor(X).astype(np.int64), np.floor(Y).astype(np.int64)
    for dk in range(span + 1):
        kk = f0 + dk
        sel = kk <= f1
        q = np.empty((int(sel.sum()), 3), dtype=np.int64)
        q[:, i], q[:, j], q[:, k] = ii[sel], jj[sel], kk[sel]
        marked[_flat(q, shape)] = True


def _flat(q: np.ndarray, shape: np.ndarray) -> np.ndarray:
    q = np.clip(q, 0, shape - 1)
    return (q[:, 0] * shape[1] + q[:, 1]) * shape[2] + q[:, 2]


def _label(free: np.ndarray):
    from scipy import ndimage
    st = ndimage.generate_binary_structure(3, 1)             # six neighbours: no corner leaks
    lab, n = ndimage.label(free, structure=st)
    return lab, int(n)


def _border_labels(lab: np.ndarray) -> set[int]:
    faces = [lab[0], lab[-1], lab[:, 0], lab[:, -1], lab[:, :, 0], lab[:, :, -1]]
    out: set[int] = set()
    for f in faces:
        out.update(np.unique(f).tolist())
    out.discard(0)
    return out


def _neighbours(idx: np.ndarray, shape) -> np.ndarray:
    """The six neighbours of each flat cube index (inside the grid), flat."""
    sx, sy, sz = shape
    i = idx // (sy * sz)
    j = (idx // sz) % sy
    k = idx % sz
    out = []
    for di, dj, dk in _SIX:
        ii, jj, kk = i + di, j + dj, k + dk
        ok = (ii >= 0) & (ii < sx) & (jj >= 0) & (jj < sy) & (kk >= 0) & (kk < sz)
        out.append((ii[ok] * sy + jj[ok]) * sz + kk[ok])
    return np.concatenate(out) if out else np.zeros(0, dtype=np.int64)


def _dilate(idx: np.ndarray, shape) -> np.ndarray:
    return np.unique(np.r_[idx, _neighbours(idx, shape)])


# ------------------------------------------------------------------------------ the caps ----
@dataclass
class _Loop:
    vidx: np.ndarray
    eidx: np.ndarray
    P: np.ndarray
    c: np.ndarray
    n: np.ndarray
    area: float
    dev: float
    wh: tuple[float, float]
    kind: str
    cap: np.ndarray | None = None     # flat cube indices of its cap
    #: points on the cap's surface away from its rim, each with the cap's normal there: what lies
    #: on either side of the cap is read just off these
    probe: np.ndarray | None = None
    probe_n: np.ndarray | None = None
    active: bool = True

    @property
    def d(self) -> float:
        return 2.0 * math.sqrt(max(self.area, 0.0) / math.pi)


def _cap_cells(lp: _Loop, grid: Grid) -> np.ndarray:
    """The cubes of a loop's cap: the flat disc of its plane inside it, or - a loop that strays from
    its plane - the lid builder's own surface (cad/lids). Thickened by one cube so the flood cannot
    slip past its edge where it meets the wall. Sets the loop's probe points."""
    from meshpipeline.cad.lids import LidError, lid
    if lp.dev > CURVED_LOOP * max(lp.d, 1e-30):
        try:
            extra, tris, _info = lid(lp.P)
            pts = np.vstack([lp.P, extra]) if len(extra) else lp.P
            T = pts[np.asarray(tris, dtype=np.int64)]
            nn = np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0])
            nl = np.linalg.norm(nn, axis=1)
            ok = nl > 0
            # the lid winds with the loop, whose normal is lp.n: its triangles face that way
            pick = np.linspace(0, int(ok.sum()), min(int(ok.sum()), MAX_PROBES), endpoint=False).astype(int)
            lp.probe = T[ok].mean(axis=1)[pick]
            lp.probe_n = (nn[ok] / nl[ok][:, None])[pick]
            return _dilate(raster(T, grid), grid.shape)
        except (LidError, ValueError, IndexError):
            pass
    u = np.cross(lp.n, [0.0, 0.0, 1.0] if abs(lp.n[2]) < 0.9 else [0.0, 1.0, 0.0])
    u /= np.linalg.norm(u)
    w = np.cross(lp.n, u)
    xy = np.c_[(lp.P - lp.c) @ u, (lp.P - lp.c) @ w]
    step = 0.7 * grid.h
    q = _fill_polygon(xy, step)
    pts = lp.c + np.outer(q[:, 0], u) + np.outer(q[:, 1], w)
    pick = pts[np.linspace(0, len(pts), min(len(pts), MAX_PROBES), endpoint=False).astype(int)] if len(pts) else pts
    lp.probe = pick
    lp.probe_n = np.repeat(lp.n[None, :], len(pick), axis=0)
    # the rim itself, densely: the cap meets the wall all the way round
    seg = np.roll(lp.P, -1, axis=0) - lp.P
    m = np.maximum(np.ceil(np.linalg.norm(seg, axis=1) / step), 1).astype(int)
    rim = np.concatenate([lp.P[i] + np.outer(np.linspace(0, 1, m[i], endpoint=False), seg[i])
                          for i in range(len(lp.P))])
    allp = np.vstack([pts, rim])
    cells = _flat(np.floor((allp - grid.origin) / grid.h).astype(np.int64), np.asarray(grid.shape))
    return _dilate(np.unique(cells), grid.shape)


def _fill_polygon(xy: np.ndarray, step: float) -> np.ndarray:
    """Points on a square lattice of `step` inside the closed 2D polygon (even-odd rule), by rows:
    each row's crossings with the polygon's edges, sorted, paired."""
    lo, hi = xy.min(axis=0), xy.max(axis=0)
    ys = np.arange(lo[1] + 0.5 * step, hi[1], step)
    if not len(ys):
        return np.zeros((0, 2))
    a, b = xy, np.roll(xy, -1, axis=0)
    out = []
    for r0 in range(0, len(ys), 256):
        y = ys[r0:r0 + 256][:, None]
        crosses = (a[None, :, 1] > y) != (b[None, :, 1] > y)
        with np.errstate(divide="ignore", invalid="ignore"):
            x = a[None, :, 0] + (y - a[None, :, 1]) * (b[None, :, 0] - a[None, :, 0]) / (b[None, :, 1] - a[None, :, 1])
        x = np.where(crosses, x, np.inf)
        x.sort(axis=1)
        for i in range(len(y)):
            row = x[i][np.isfinite(x[i])]
            for x0, x1 in zip(row[0::2], row[1::2]):
                xs = np.arange(x0 + 0.5 * step, x1, step)
                if len(xs):
                    out.append(np.c_[xs, np.full(len(xs), float(y[i, 0]))])
    return np.vstack(out) if out else np.zeros((0, 2))


def _loops(mesh, grid: Grid) -> tuple[list[_Loop], int]:
    """Every loop of the surface that could be the edge of an opening (cad/open_ends: the open
    rims, and the outline of every smooth patch), measured; a loop a face lies across is the
    outline of that face, never an opening. Returns (the loops the grid can see, how many it
    cannot - too small for it)."""
    from meshpipeline.cad.open_ends import MIN_LOOP, _measure
    out: list[_Loop] = []
    small = 0
    for found in mesh.feature_loops():
        vidx, eidx = found
        P = mesh.verts[vidx]
        m = _measure(P)
        if m is None:
            continue
        c, n, area, wh, dev = m
        d = 2.0 * math.sqrt(area / math.pi)
        if d < MIN_LOOP * mesh.diag:
            continue
        if mesh.shape(found) is None and mesh.why.startswith("covered"):
            continue
        rim = bool(np.all(mesh.count[eidx] == 1))
        lp = _Loop(vidx=np.asarray(vidx), eidx=np.asarray(eidx), P=P, c=c, n=n, area=area, dev=dev,
                   wh=wh, kind="rim" if rim else "bore")
        if d < MIN_ACROSS * grid.h:
            small += 1
            continue
        out.append(lp)
    out.sort(key=lambda q: (-q.area, float(q.c[0]), float(q.c[1]), float(q.c[2])))
    return out, small


# ------------------------------------------------------------------------------ the map ----
def read_inside(mesh=None, *, tris=None, cells: int = GRID_CELLS, h_max: float | None = None) -> InsideMap:
    """The inside-space map of a surface (an open_ends surface, or triangles (n, 3, 3))."""

    from meshpipeline.cad.open_ends import surface
    if mesh is None:
        mesh = surface(tris)
    if mesh is None:
        raise ValueError("too few triangles to map")
    V, F = mesh.verts, mesh.faces
    grid = Grid.around(V.min(axis=0), V.max(axis=0), cells=cells, h_max=h_max)
    shape = grid.shape
    wall = np.zeros(grid.n, dtype=bool)
    wall[raster(V[F], grid)] = True
    # THE SPACE NO CAP IS NEEDED TO CLOSE: inside a closed shell (metal, or a body that is the fluid)
    lab0, _n0 = _label(~wall.reshape(shape))
    out0 = _border_labels(lab0)
    enclosed = (lab0 > 0) & ~np.isin(lab0, list(out0))
    open_air = np.isin(lab0, list(out0))
    del lab0
    loops, small = _loops(mesh, grid)
    if len(loops) > MAX_LOOPS:
        # MORE LOOPS THAN THE MAP CAPS (a perforated plate, a bed of spheres): a map of some of
        # them would leave the others open and read the part's inside wrong - the map says it
        # cannot read the part, and the shape rules stand alone
        raise PartialMap(f"{len(loops)} loops the grid can see, more than the {MAX_LOOPS} the map caps")
    for lp in loops:
        lp.cap = _cap_cells(lp, grid)
    notes: list[str] = []
    oa = open_air.ravel()
    lab = np.zeros(shape, dtype=np.int32)
    outside: set[int] = set()
    sides: dict[int, tuple[int, int, bool]] = {}
    pocket_ids: set[int] = set()
    sizes = np.zeros(1, dtype=np.int64)
    for _round in range(MAX_ROUNDS):
        blocked = wall.copy()
        for lp in loops:
            if lp.active and lp.cap is not None and len(lp.cap):
                blocked[lp.cap] = True
        lab, n_lab = _label(~blocked.reshape(shape))
        outside = _border_labels(lab)
        flat_lab = lab.ravel()
        sizes = np.bincount(flat_lab, minlength=n_lab + 1)
        # A POCKET is space the open air reached with no cap at all (else it is a closed shell's
        # inside: the metal of a solid wall, or a body that is the fluid) and reaches now only
        # through a cap
        in_air = np.bincount(flat_lab, weights=oa, minlength=n_lab + 1)
        pocket_ids = {g for g in range(1, n_lab + 1) if g not in outside and sizes[g] >= MIN_POCKET_CELLS
                      and in_air[g] >= 0.5 * sizes[g]}
        split = False
        sides = {}
        for k, lp in enumerate(loops):
            if not lp.active or lp.cap is None or not len(lp.cap):
                continue
            a, b = _side_labels(lp, flat_lab, grid, outside, pocket_ids)
            kind_a = "out" if a in outside else "pocket" if a in pocket_ids else ""
            kind_b = "out" if b in outside else "pocket" if b in pocket_ids else ""
            if {kind_a, kind_b} == {"out", "pocket"}:
                # A MOUTH: open air on one side of the cap, a pocket on the other
                pocket, out_plus = (b, True) if kind_a == "out" else (a, False)
                sides[k] = (pocket, a if out_plus else b, out_plus)
                continue
            # anything else closes nothing that is the part's inside meeting the outside: a cap
            # with a pocket on both sides (a step in a bore, a barb's ridge across a hose tail, a
            # thread's groove) splits one pocket in two and is taken away
            lp.active = False
            if kind_a == kind_b == "pocket" and a != b:
                split = True
        if split:
            continue
        # THE DENTS, judged only once no cap splits a pocket (so the space behind a mouth is the
        # whole pocket, never the slice up to a thread groove's cap): a mouth whose space behind it
        # is shallow against its width is the lid of a dent in the part's outside (a stamped
        # flange's dished back), and is taken away
        dent = False
        for k in sorted(sides):
            pocket, _out, out_plus = sides[k]
            if _shallow(loops[k], -1.0 if out_plus else 1.0, pocket, flat_lab, grid):
                loops[k].active = False
                dent = True
        if not dent:
            break
    flat_lab = lab.ravel()
    # the mouths: the tighter of two caps closing one pocket at one place
    mouth_rows = [(k, [sides[k][0]]) for k in sorted(sides)]
    outn = {k: (loops[k].n if sides[k][2] else -loops[k].n) for k in sides}
    mouth_rows = _tightest(loops, mouth_rows, outn, grid.h)
    pocket_ids = {g for _k, ps in mouth_rows for g in ps}
    # ONE LINING, ONE PASSAGE: pockets the grid splits at a throat narrower than its cubes (a
    # nozzle's orifice) are lined, on the same side, by one piece of the surface cut along the
    # mouths - the fluid runs on between them - and are one pocket
    groups = _by_lining(mesh, loops, mouth_rows, flat_lab, grid, sorted(pocket_ids))
    h3 = grid.h ** 3
    gsize = {r: sum(int(sizes[g]) for g in ms) for r, ms in groups.items()}
    largest = max(gsize.values(), default=1)
    pockets: list[Pocket] = []
    pid_of: dict[int, int] = {}
    for r in sorted(groups, key=lambda r: (-gsize[r], r)):
        pid = len(pockets)
        for g in groups[r]:
            pid_of[g] = pid
        # over every grid region of the passage: a short wide chamber beyond a throat the grid
        # blocks can hold the point farthest from every wall
        deepest, clear = _deepest(np.isin(lab, groups[r]), grid)
        pockets.append(Pocket(id=pid, cells=gsize[r], volume=float(gsize[r]) * h3, deepest=deepest,
                              clearance=clear, share=float(gsize[r]) / float(largest)))
    mouths: list[Mouth] = []
    for k, ps in mouth_rows:
        lp = loops[k]
        mine = tuple(sorted({pid_of[g] for g in ps if g in pid_of}))
        if not mine:
            continue
        # out of the pocket: toward the side the open air is on
        n = lp.n.copy() if sides[k][2] else -lp.n
        mid = len(mouths)
        mouths.append(Mouth(id=mid, kind=lp.kind, vidx=lp.vidx, eidx=lp.eidx, centroid=lp.c, normal=n,
                            area=lp.area, wh=lp.wh, planarity=lp.dev / max(lp.d, 1e-30), pockets=mine,
                            cells=int(len(lp.cap)) if lp.cap is not None else 0))
        for p in mine:
            pockets[p].mouths.append(mid)
    if small:
        notes.append(f"{small} loop(s) too small for the grid's {grid.h:.3g} cubes are read by the hole finder")
    e_deep, e_clear = (None, 0.0)
    if enclosed.any():
        e_deep, e_clear = _deepest(enclosed, grid)
    return InsideMap(h=grid.h, origin=grid.origin, shape=shape, pockets=pockets, mouths=mouths,
                     enclosed_cells=int(enclosed.sum()), loops_capped=len(loops), loops_small=small,
                     notes=notes, enclosed_deepest=e_deep, enclosed_clearance=e_clear,
                     labels=flat_lab.astype(np.int32), outside_labels=frozenset(outside), shell=enclosed.ravel(),
                     pocket_labels={pid: sorted(g for g in pid_of if pid_of[g] == pid) for pid in range(len(pockets))})


#: A surface with more triangles than this is not mapped where the map is a reading for the stage
#: (the check's time box): the hole finder's reading stands, and the record says so.
MAP_MAX_TRIANGLES = 2_000_000
#: The leak test's grid (INS-5): a staged boundary is closed already, so a coarser grid will do.
LEAK_CELLS = 2_000_000
#: The grid's cube count follows the surface's detail: this many cubes a triangle, never fewer
#: than MIN_GRID_CELLS nor more than GRID_CELLS.
CELLS_PER_TRIANGLE = 400
MIN_GRID_CELLS = 1_500_000


def read_inside_or_none(mesh, *, cells: int = GRID_CELLS, why: list | None = None) -> InsideMap | None:
    """The map, or None when the surface is too big to map in the check's time or cannot be read:
    never a reason to fail the caller (the old readings stand)."""
    import logging
    try:
        if mesh is None or len(mesh.faces) > MAP_MAX_TRIANGLES:
            if why is not None:
                why.append("the surface was too big to map")
            return None
        # the grid as fine as the tessellation's own detail asks, up to the budget: a coarse
        # surface of a few thousand triangles has no loop a finer grid would see
        cells = int(min(cells, max(MIN_GRID_CELLS, CELLS_PER_TRIANGLE * len(mesh.faces))))
        return read_inside(mesh, cells=cells)
    except PartialMap as exc:
        if why is not None:
            why.append(str(exc))
        return None
    except Exception as exc:  # noqa: BLE001 - a reading that fails leaves the old ones
        logging.getLogger(__name__).warning("inside-space map failed (%s: %s)", type(exc).__name__, exc)
        if why is not None:
            why.append(f"the map could not be read ({type(exc).__name__})")
        return None


#: A cap with less space behind it than this share of its width (most of the cap) closes a dent.
DENT_DEPTH = 0.1


def _shallow(lp: _Loop, sgn: float, pocket: int, flat_lab: np.ndarray, grid: Grid) -> bool:
    """Whether the pocket behind a cap (on its `sgn` side) runs less deep than DENT_DEPTH of the
    cap's width under nine in ten of the cap's probe points."""
    if lp.probe is None or lp.probe_n is None or not len(lp.probe):
        return False
    reach = DENT_DEPTH * lp.d
    n_steps = int(math.ceil(reach / grid.h)) + 2
    if n_steps <= 3:
        return False                      # the grid cannot tell: a small cap is not a dent's lid
    shape = np.asarray(grid.shape)
    depth = np.zeros(len(lp.probe))
    alive = np.ones(len(lp.probe), dtype=bool)
    started = np.zeros(len(lp.probe), dtype=bool)
    for s in range(1, n_steps + 1):
        q = lp.probe + sgn * (s + 1.0) * grid.h * lp.probe_n
        ijk = np.floor((q - grid.origin) / grid.h).astype(np.int64)
        inside = np.all((ijk >= 0) & (ijk < shape), axis=1)
        lab = np.zeros(len(q), dtype=np.int64)
        lab[inside] = flat_lab[(ijk[inside, 0] * shape[1] + ijk[inside, 1]) * shape[2] + ijk[inside, 2]]
        here = lab == pocket
        started |= here
        # the run ends where the pocket does (after it began: the cap's own cubes come first)
        alive &= here | ~started
        depth = np.where(alive & here, (s + 1.0) * grid.h, depth)
    if not started.any():
        return False
    return float(np.quantile(depth[started], 0.9)) < reach


#: What lies on a side of a cap is read this many cubes off it, the first that is not wall or cap.
SIDE_STEPS = (2.0, 3.0, 4.5, 6.5)
#: ...and on a side of a face of the surface, this many.
FACE_STEPS = (1.0, 1.5, 2.5, 3.5)


def _sample(points: np.ndarray, normals: np.ndarray, sgn: float, steps, flat_lab: np.ndarray, grid: Grid,
            keep: np.ndarray) -> np.ndarray:
    """The label each point reads stepped off along its normal: the first of `steps` (in cubes)
    that lands on a kept label; 0 when none does."""
    shape = np.asarray(grid.shape)
    got = np.zeros(len(points), dtype=np.int64)
    for step in steps:
        todo = np.flatnonzero(got == 0)
        if not len(todo):
            break
        q = points[todo] + sgn * step * grid.h * normals[todo]
        ijk = np.floor((q - grid.origin) / grid.h).astype(np.int64)
        inside = np.all((ijk >= 0) & (ijk < shape), axis=1)
        lab = np.zeros(len(q), dtype=np.int64)
        lab[inside] = flat_lab[(ijk[inside, 0] * shape[1] + ijk[inside, 1]) * shape[2] + ijk[inside, 2]]
        got[todo] = np.where(np.isin(lab, keep), lab, 0)
    return got


def _by_lining(mesh, loops: list[_Loop], rows, flat_lab: np.ndarray, grid: Grid,
               pocket_ids: list[int]) -> dict[int, list[int]]:
    """The pockets in groups: two pockets lined on the same side by one piece of the surface (cut
    along the mouths' loops), each by at least LINING_SHARE of the surface round it, are one."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    groups = {g: [g] for g in pocket_ids}
    if len(pocket_ids) < 2:
        return groups
    nf = len(mesh.faces)
    cut = np.zeros(len(mesh.count), dtype=bool)
    for k, _ps in rows:
        cut[loops[k].eidx] = True
    join = (mesh.count == 2) & ~cut & (mesh.f1 >= 0)
    g_ = coo_matrix((np.ones(int(join.sum())), (mesh.f0[join], mesh.f1[join])), shape=(nf, nf))
    _, piece = connected_components(g_, directed=False)
    keep = np.asarray(pocket_ids, dtype=np.int64)
    rows_g, rows_key, rows_a = [], [], []
    for side, sgn in ((0, 1.0), (1, -1.0)):
        lab = _sample(mesh.centre, mesh.normal, sgn, FACE_STEPS, flat_lab, grid, keep)
        hit = np.flatnonzero(lab > 0)
        rows_g.append(lab[hit])
        rows_key.append(piece[hit] * 2 + side)
        rows_a.append(mesh.area[hit])
    G = np.concatenate(rows_g)
    K = np.concatenate(rows_key)
    A = np.concatenate(rows_a)
    if not len(G):
        return groups
    pairs, inv = np.unique(np.c_[G, K], axis=0, return_inverse=True)
    area = np.bincount(np.asarray(inv).reshape(-1), weights=A)
    total: dict[int, float] = {}
    for (g, _k), a in zip(pairs.tolist(), area.tolist()):
        total[g] = total.get(g, 0.0) + a
    parent = {g: g for g in pocket_ids}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    lined: dict[int, int] = {}
    for (g, key), a in zip(pairs.tolist(), area.tolist()):
        if a < LINING_SHARE * total[g]:
            continue
        if key in lined:
            ra, rb = find(lined[key]), find(g)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
        else:
            lined[key] = g
    out: dict[int, list[int]] = {}
    for g in pocket_ids:
        out.setdefault(find(g), []).append(g)
    return out


def _side_labels(lp: _Loop, flat_lab: np.ndarray, grid: Grid, outside: set[int],
                 pockets: set[int]) -> tuple[int, int]:
    """The space on each side of a cap, by its probe points stepped off along the cap's normal:
    (the label most of the + side reads, the label most of the - side reads); 0 for a side all
    wall. Only open air and pockets count (a closed shell's inside is the part's own)."""
    if lp.probe is None or lp.probe_n is None or not len(lp.probe):
        return 0, 0
    shape = np.asarray(grid.shape)
    out = []
    for sgn in (1.0, -1.0):
        got = np.zeros(len(lp.probe), dtype=np.int64)
        for step in SIDE_STEPS:
            todo = got == 0
            if not todo.any():
                break
            q = lp.probe[todo] + sgn * step * grid.h * lp.probe_n[todo]
            ijk = np.floor((q - grid.origin) / grid.h).astype(np.int64)
            inside = np.all((ijk >= 0) & (ijk < shape), axis=1)
            lab = np.zeros(len(q), dtype=np.int64)
            lab[inside] = flat_lab[(ijk[inside, 0] * shape[1] + ijk[inside, 1]) * shape[2] + ijk[inside, 2]]
            lab = np.where(np.isin(lab, list(outside | pockets)), lab, 0)
            idx = np.flatnonzero(todo)
            got[idx] = lab
        vals = got[got > 0]
        if not len(vals):
            out.append(0)
            continue
        u, c = np.unique(vals, return_counts=True)
        out.append(int(u[np.argmax(c)]))               # ties: the lowest label (np.unique sorts)
    return out[0], out[1]


def _tightest(loops: list[_Loop], rows: list[tuple[int, list[int]]], outn: dict | None = None,
              h: float | None = None) -> list[tuple[int, list[int]]]:
    """Of mouth caps that close one pocket at one place - their caps share most of the smaller's
    cubes - the smallest loop is the mouth (a bore's mouth, not the end face's outline round it)."""
    keep = []
    order = sorted(rows, key=lambda r: (loops[r[0]].area, r[0]))
    taken: list[tuple[set, np.ndarray]] = []
    for k, ps in order:
        cap = loops[k].cap
        assert cap is not None
        dup = False
        for tps, tcap in taken:
            if not (set(ps) & tps):
                continue
            shared = np.intersect1d(tcap, cap, assume_unique=True)
            if len(shared) >= 0.5 * min(len(tcap), len(cap)):
                dup = True
                break
        if not dup:
            keep.append((k, ps))
            taken.append((set(ps), cap))
    # STACKED MOUTHS: of two that open one pocket facing the same way on one axis, one just behind
    # the other (the inner edge of a hole through a thick wall, a counterbore's step), the one in
    # front is the mouth - the finder's own rule (cad/open_ends._distinct)
    gone: set[int] = set()
    for a, pa in keep:
        for b, pb in keep:
            if a == b or a in gone or b in gone or not (set(pa) & set(pb)):
                continue
            la, lb = loops[a], loops[b]
            # the same way OUT of the pocket: the two ends of a short passage face apart and are
            # two mouths, never one
            na = outn.get(a, la.n) if outn else la.n
            nb = outn.get(b, lb.n) if outn else lb.n
            if float(na @ nb) < 0.95:
                continue
            big = max(la.d, lb.d)
            r = la.c - lb.c
            along = float(r @ lb.n)
            side = float(np.linalg.norm(r - along * lb.n))
            # closer along the axis than the grid can part (two cubes), any two sizes are one mouth
            # (a nozzle's throat a tenth of a millimetre behind its chevrons' edge); farther apart,
            # only two of about one size are (a counterbore, a hole's inner edge)
            hair = abs(along) <= 2.0 * (h or 0.0)
            if not hair and not 0.5 <= la.area / max(lb.area, 1e-300) <= 2.0:
                continue
            if side <= 0.2 * big and abs(along) <= big:
                if hair:
                    gone.add(a if la.area < lb.area else b)       # the wider edge is in front
                else:
                    # the one farther along the way out of the pocket is in front
                    out_b = outn.get(b, lb.n) if outn else lb.n
                    gone.add(a if float(r @ out_b) < 0 else b)
    return sorted([r for r in keep if r[0] not in gone], key=lambda r: r[0])


def _deepest(mask: np.ndarray, grid: Grid) -> tuple[np.ndarray, float]:
    """The pocket's cube farthest from every cube not in it (its walls, its caps), as a point, and
    that distance less half a cube. Ties go to the first cube in grid order: deterministic."""
    from scipy import ndimage
    ii = np.flatnonzero(mask.any(axis=(1, 2)))
    jj = np.flatnonzero(mask.any(axis=(0, 2)))
    kk = np.flatnonzero(mask.any(axis=(0, 1)))
    sub = mask[ii[0]:ii[-1] + 1, jj[0]:jj[-1] + 1, kk[0]:kk[-1] + 1]
    pad = np.pad(sub, 1)
    dist = ndimage.distance_transform_edt(pad)
    at = int(np.argmax(dist))
    i, j, k = np.unravel_index(at, dist.shape)
    ijk = (i - 1 + ii[0], j - 1 + jj[0], k - 1 + kk[0])
    return grid.centre(ijk), max(float(dist.flat[at]) - 0.5, 0.0) * grid.h


# ---------------------------------------------------------------------------- the leak test ----
@dataclass
class Leak:
    """The staged boundary does not hold the fluid: the flood from the seed reached the open air."""
    point: np.ndarray               # where it got out, about
    width: float                    # how wide the way out is, about (a cube's multiple)


def leak_test(V: np.ndarray, F: np.ndarray, seed, *, cells: int = GRID_CELLS) -> tuple[Leak | None, dict]:
    """Flood a staged fluid boundary from its seed: (None, facts) when the flood stays inside, else
    (the leak, facts). The leak is located by thickening the wall a cube at a time until the flood
    stays in: the cubes the thin flood reached and the thick one did not, next to the thick one's
    edge, are the way out."""
    from scipy import ndimage
    grid = Grid.around(V.min(axis=0), V.max(axis=0), cells=cells)
    shape = grid.shape
    wall = np.zeros(grid.n, dtype=bool)
    wall[raster(V[F], grid)] = True
    s = grid.index(seed)
    facts: dict = {"cell_m": round(grid.h, 9), "grid": list(shape)}
    W = wall.reshape(shape)
    if W[s]:
        facts["seed_in_wall"] = True
        # the nearest free cube to the seed stands in for it
        free = np.argwhere(~W)
        if not len(free):
            return None, facts
        f_ = free[np.argmin(np.linalg.norm(free - np.asarray(s), axis=1))]
        s = (int(f_[0]), int(f_[1]), int(f_[2]))
    lab, _n = _label(~W)
    me = int(lab[s])
    border = _border_labels(lab)
    facts["flood_cells"] = int(np.sum(lab == me))
    if me not in border:
        facts["sealed"] = True
        return None, facts
    facts["sealed"] = False
    reached = lab == me
    st = ndimage.generate_binary_structure(3, 1)
    thick = W.copy()
    for r in range(1, 5):
        thick = ndimage.binary_dilation(thick, structure=st)
        if thick[s]:
            break
        lab_r, _ = _label(~thick)
        mr = int(lab_r[s])
        if mr not in _border_labels(lab_r):
            inside = ndimage.binary_dilation(lab_r == mr, structure=st, iterations=r + 1)
            gap = reached & ~inside & ndimage.binary_dilation(inside, structure=st)
            pts = np.argwhere(gap)
            if len(pts):
                p = grid.centre(pts.mean(axis=0))
                return Leak(point=p, width=(2 * r - 1) * grid.h), facts
            break
    # a wide way out: where the flood leaves the seed's own region, about - the reached cube nearest
    # the seed that touches the grid's border region is no help, so the farthest-reaching cube of
    # the flood inside the boundary's box is named instead
    lo, hi = V.min(axis=0), V.max(axis=0)
    pts = np.argwhere(reached)
    cen = grid.centre(pts)
    # a wide way out: the flood's cubes on the boundary's own box, inside it - where the space
    # inside crosses out of the part's extent - the one nearest the seed
    inb = np.all((cen >= lo - 0.5 * grid.h) & (cen <= hi + 0.5 * grid.h), axis=1)
    on_box = inb & np.any((cen <= lo + grid.h) | (cen >= hi - grid.h), axis=1)
    if on_box.any():
        c = cen[on_box]
        p = c[int(np.argmin(np.linalg.norm(c - np.asarray(seed, dtype=float), axis=1)))]
    else:
        p = cen[0]
    return Leak(point=p, width=math.inf), facts


__all__ = ["GRID_CELLS", "MAP_MAX_TRIANGLES", "MIN_ACROSS", "PASSAGE_SHARE", "Grid", "InsideMap", "Leak", "PartialMap",
           "Mouth", "Pocket", "leak_test", "raster", "read_inside", "read_inside_or_none"]
