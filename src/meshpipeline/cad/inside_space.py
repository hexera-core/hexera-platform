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
    #: INS-8: the cubes that closed the surface's leaks (flat, grid order), when the map was read
    #: with them closed (`close`, the closing's radius in cubes); None for the map as drawn
    closed: np.ndarray | None = field(default=None, repr=False)
    close: int = 0
    #: ...and the leaks among them: each gap between a passage and space that is not it
    leaks: list = field(default_factory=list)

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

    def leak_at(self, point, reach: float) -> bool:
        """Whether one of the leaks this map closed (INS-8) lies within `reach` of the point: a
        small hole there is a leak the closing closed, not an opening."""
        return any(self.leak_distance(s, point) <= reach for s in self.leaks)

    def leak_distance(self, s, point) -> float:
        """How far a leak's nearest closed cube (its centre) is from the point."""
        p = np.asarray(point, dtype=float)
        c = s.cubes
        if not len(c):
            return float(np.linalg.norm(np.asarray(s.point, dtype=float) - p))
        sy, sz = self.shape[1], self.shape[2]
        ijk = np.stack([c // (sy * sz), (c // sz) % sy, c % sz], axis=1)
        cen = self.origin + (ijk + 0.5) * self.h
        return float(np.min(np.linalg.norm(cen - p, axis=1)))

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
def read_inside(mesh=None, *, tris=None, cells: int = GRID_CELLS, h_max: float | None = None,
                close: int = 0) -> InsideMap:
    """The inside-space map of a surface (an open_ends surface, or triangles (n, 3, 3)).

    `close` > 0 reads the map with the surface's LEAKS closed (INS-8): every gap in the wall up to
    2 x `close` cubes wide is filled before the flood, except where the open ends that are caps'
    loops are (an opening is never closed as a leak); the cubes filled are kept on the map
    (`InsideMap.closed`). 0 is the map as drawn."""

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
    loops, small = _loops(mesh, grid)
    if len(loops) > MAX_LOOPS:
        # MORE LOOPS THAN THE MAP CAPS (a perforated plate, a bed of spheres): a map of some of
        # them would leave the others open and read the part's inside wrong - the map says it
        # cannot read the part, and the shape rules stand alone
        raise PartialMap(f"{len(loops)} loops the grid can see, more than the {MAX_LOOPS} the map caps")
    for lp in loops:
        lp.cap = _cap_cells(lp, grid)
    closed = None
    drawn = wall
    if close > 0:
        added = _leak_fill(mesh, wall, grid, int(close))
        wall = wall | added
        closed = np.flatnonzero(added)
    # THE SPACE NO CAP IS NEEDED TO CLOSE: inside a closed shell (metal, or a body that is the fluid)
    lab0, _n0 = _label(~wall.reshape(shape))
    out0 = _border_labels(lab0)
    enclosed = (lab0 > 0) & ~np.isin(lab0, list(out0))
    open_air = np.isin(lab0, list(out0))
    del lab0
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
    pocket_labels = {pid: sorted(g for g in pid_of if pid_of[g] == pid) for pid in range(len(pockets))}
    spots: list[LeakSpot] = []
    if closed is not None and len(closed):
        # the caps left standing close their own loops: a closed cube under a cap (the thickened
        # rim of an opening) closes nothing the cap does not
        capped = np.zeros(grid.n, dtype=bool)
        for lp in loops:
            if lp.active and lp.cap is not None and len(lp.cap):
                capped[lp.cap] = True
        # the leaks are between the part's inside and what is not it: its passages, or - a body
        # that is the fluid, with no passage - the inside of its closed shell
        inner = [g for p in pockets if p.passage for g in pocket_labels[p.id]]
        if not inner and enclosed.any():
            inner = [int(g) for g in np.unique(flat_lab[enclosed.ravel()]) if g > 0]
        spots = _leak_spots(drawn, closed[~capped[closed]], flat_lab, grid, inner, V[F])
    return InsideMap(h=grid.h, origin=grid.origin, shape=shape, pockets=pockets, mouths=mouths,
                     enclosed_cells=int(enclosed.sum()), loops_capped=len(loops), loops_small=small,
                     notes=notes, enclosed_deepest=e_deep, enclosed_clearance=e_clear,
                     labels=flat_lab.astype(np.int32), outside_labels=frozenset(outside), shell=enclosed.ravel(),
                     pocket_labels=pocket_labels, closed=closed, close=int(close), leaks=spots)


@dataclass
class LeakSpot:
    """One leak the closing closed (INS-8): a gap in the wall between a passage and space that is
    not that passage (the open air, the part's material, another pocket)."""
    id: int
    point: np.ndarray               # its middle, about: the closed cube farthest from the wall
    width: float                    # how wide the gap is, about (a cube's multiple)
    cubes: np.ndarray = field(repr=False)   # the closed cubes that close it (flat, grid order)
    passage: tuple[int, ...] = ()   # the passage labels on its fluid side
    #: "gap" (closed cubes of the grid) or "hole" (a hole of the surface too small for the grid to
    #: see, read by the shape rules: its width is its own size)
    kind: str = "gap"

    def as_dict(self, mm_per_unit: float = 1000.0) -> dict:
        return {"id": self.id, "kind": self.kind, "width_mm": round(self.width * mm_per_unit, 3),
                "at_mm": [round(float(v) * mm_per_unit, 2) for v in self.point], "cubes": int(len(self.cubes))}


def _bottleneck(cubes: np.ndarray, nb: np.ndarray, flat_lab: np.ndarray, dist: np.ndarray, shape,
                passage: set) -> tuple[float, int]:
    """How wide a leak is, in cubes of clearance: the widest way through it from its passage to the
    space beyond - the largest clearance from the drawn wall (`dist`) that every cube of some path
    of its cubes keeps (the cubes are let in from the clearest down until the passage and the space
    beyond first join). Returns that clearance and the cube where they joined (its middle)."""
    from scipy import ndimage
    sy, sz = int(shape[1]), int(shape[2])
    nb = np.unique(nb)
    lab_nb = flat_lab[nb]
    src = nb[np.isin(lab_nb, list(passage))]
    dst = nb[(lab_nb > 0) & ~np.isin(lab_nb, list(passage))]
    every = np.unique(np.r_[cubes, src, dst])
    ijk = np.stack([every // (sy * sz), (every // sz) % sy, every % sz], axis=1)
    lo = ijk.min(axis=0)
    box = tuple(int(v) for v in ijk.max(axis=0) - lo + 1)

    def local(c):
        q = np.stack([c // (sy * sz), (c // sz) % sy, c % sz], axis=1) - lo
        return (q[:, 0] * box[1] + q[:, 1]) * box[2] + q[:, 2]

    ls, ld, lc = local(src), local(dst), local(cubes)
    dc = dist[cubes]
    st = ndimage.generate_binary_structure(3, 1)
    for level in sorted(set(np.round(dc, 6).tolist()), reverse=True):
        m = np.zeros(int(np.prod(box)), dtype=bool)
        m[ls] = True
        m[ld] = True
        inn = dc >= level - 1e-9
        m[lc[inn]] = True
        lab, _n = ndimage.label(m.reshape(box), structure=st)
        lab = lab.ravel()
        joined = set(np.unique(lab[ls]).tolist()) & set(np.unique(lab[ld]).tolist())
        joined.discard(0)
        if joined:
            on = inn & np.isin(lab[lc], list(joined)) & (np.abs(dc - level) <= 1e-9)
            at = int(cubes[np.flatnonzero(on)[0]]) if on.any() else int(cubes[int(np.argmax(dc))])
            return float(level), at
    return float(dc.min()), int(cubes[int(np.argmin(dc))])


def _surface_distance(points: np.ndarray, T: np.ndarray, tree, k: int = 32) -> np.ndarray:
    """The exact distance from each point to the nearest of the triangles T (n, 3, 3), over the k
    triangles whose centres are nearest it (`tree`: a cKDTree of the triangles' centres)."""
    if not len(points) or not len(T):
        return np.full(len(points), np.inf)
    k = min(k, len(T))
    _d, nn = tree.query(points, k=k)
    nn = np.asarray(nn).reshape(len(points), -1)
    best = np.full(len(points), np.inf)
    for col in range(nn.shape[1]):
        q = _closest_on_triangles(points, T[nn[:, col]])
        best = np.minimum(best, np.linalg.norm(q - points, axis=1))
    return best


def _leak_spots(drawn: np.ndarray, closed: np.ndarray, flat_lab: np.ndarray, grid: Grid,
                passage_labels: list[int], surface_tris: np.ndarray | None = None) -> list[LeakSpot]:
    """The leaks among the closed cubes: each piece of them (six neighbours) that touches a passage
    on one side and space that is not that passage on another. Its width is the widest way through
    it, read off the drawn surface (`surface_tris`, else the drawn wall's cubes): twice the
    clearance every step of that way keeps."""
    from scipy import ndimage
    shape = grid.shape
    if not passage_labels or not len(closed):
        return []
    mask = np.zeros(grid.n, dtype=bool)
    mask[closed] = True
    comp, n = ndimage.label(mask.reshape(shape), structure=ndimage.generate_binary_structure(3, 1))
    comp = comp.ravel()
    dist = ndimage.distance_transform_edt(~drawn.reshape(shape)).ravel()
    passage = {int(g) for g in passage_labels}
    out: list[LeakSpot] = []
    tree = None
    order = np.argsort(comp[closed], kind="stable")
    cs = closed[order]
    bounds = np.r_[0, np.flatnonzero(np.diff(comp[cs])) + 1, len(cs)]
    for a, b in zip(bounds[:-1], bounds[1:]):
        cubes = np.sort(cs[a:b])
        nb = _neighbours(cubes, shape)
        labs = set(np.unique(flat_lab[nb]).tolist()) - {0}
        mine = labs & passage
        if not mine or not (labs - mine):
            continue
        # the clearance of each of its cubes from the drawn surface itself, exactly: the grid's
        # own count of cubes would read a 0.8 mm pinhole the raster half covers as one cube wide
        if surface_tris is not None and len(surface_tris):
            if tree is None:
                from scipy.spatial import cKDTree
                tree = cKDTree(surface_tris.mean(axis=1))
            dist[cubes] = _surface_distance(grid.centre(np.stack(np.unravel_index(cubes, shape), axis=1)),
                                            surface_tris, tree) / grid.h
        level, at = _bottleneck(cubes, nb, flat_lab, dist, shape, mine)
        width = max(2.0 * level, 0.5) * grid.h
        out.append(LeakSpot(id=len(out), point=grid.centre(np.unravel_index(at, shape)), width=width,
                            cubes=cubes, passage=tuple(sorted(mine))))
    out.sort(key=lambda s: (-s.width, float(s.point[0]), float(s.point[1]), float(s.point[2])))
    for k, s in enumerate(out):
        s.id = k
    return out


def _leak_fill(mesh, wall: np.ndarray, grid: Grid, r: int) -> np.ndarray:
    """The cubes that close the wall's gaps up to 2r cubes wide: the wall thickened by r cubes (every
    free cube within r of it), only where the surface is open - within r + 1 cubes of an edge not
    on two faces, or of where two pieces of the surface meet (flat, grid order). Nowhere else: a
    narrow channel of a closed surface is the part's own, never a leak. A hole of a sheet, a slit,
    the gap between two pieces close when they are at most 2r cubes across; an opening wider than
    that stays open (only its rim is thickened), one narrower is closed as a leak - as the user's
    leak size says."""
    from scipy import ndimage
    shape = grid.shape
    W = wall.reshape(shape)
    added = ((ndimage.distance_transform_edt(~W) <= r) & ~W).ravel()
    if not added.any():
        return added
    # WHERE THE SURFACE IS OPEN: its edges not on two faces, drawn into the grid
    e = np.flatnonzero(mesh.count != 2)
    near = np.zeros(grid.n, dtype=bool)
    if len(e):
        near[_segment_cells(mesh.verts[mesh.ea[e]], mesh.verts[mesh.eb[e]], grid)] = True
    # ...and where two pieces of the surface come near each other (no open edge between them)
    pieces = _surface_pieces(mesh)
    if int(pieces.max()) > 0:
        own = np.full(grid.n, -1, dtype=np.int64)
        for p in range(int(pieces.max()) + 1):
            cub = raster(mesh.verts[mesh.faces[pieces == p]], grid)
            clash = own[cub] >= 0
            near[cub[clash & (own[cub] != p)]] = True
            own[cub[~clash]] = p
        Wl = own.reshape(shape)
        _d, idx = ndimage.distance_transform_edt(Wl < 0, return_indices=True)
        nearest = Wl[idx[0], idx[1], idx[2]]
        ridge = np.zeros(shape, dtype=bool)
        for ax in range(3):
            a_ = np.swapaxes(nearest, 0, ax)
            diff = a_[1:] != a_[:-1]
            r_ = np.swapaxes(ridge, 0, ax)
            r_[1:] |= diff
            r_[:-1] |= diff
        near |= (ridge & (_d <= r + 1)).ravel()
    if not near.any():
        return np.zeros(grid.n, dtype=bool)
    zone = ndimage.distance_transform_edt(~near.reshape(shape)) <= r + 1
    return added & zone.ravel()


#: At most this many pieces of a surface are told apart where the leaks are sought (the largest by
#: area); the rest - a scan's crumbs - count as one more piece.
MAX_PIECES = 64


def _surface_pieces(mesh) -> np.ndarray:
    """Each face's piece of the surface (faces joined across edges with two faces), the largest
    MAX_PIECES apart and every smaller one as one more, numbered largest first."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    n = len(mesh.faces)
    m = mesh.count == 2
    a, b = mesh.f0[m], mesh.f1[m]
    g = coo_matrix((np.ones(len(a)), (a, b)), shape=(n, n))
    _k, lab = connected_components(g, directed=False)
    area = np.bincount(lab, weights=mesh.area)
    order = np.lexsort((np.arange(len(area)), -area))      # largest first, ties by first face
    rank = np.empty(len(area), dtype=np.int64)
    rank[order] = np.arange(len(area))
    return np.minimum(rank[lab], MAX_PIECES)


def _segment_cells(a: np.ndarray, b: np.ndarray, grid: Grid) -> np.ndarray:
    """The cubes the segments a -> b pass through (sampled at a third of a cube), flat."""
    if not len(a):
        return np.zeros(0, dtype=np.int64)
    L = np.linalg.norm(b - a, axis=1)
    m = np.maximum(np.ceil(L / (grid.h / 3.0)), 1).astype(np.int64)
    idx = np.repeat(np.arange(len(a)), m + 1)
    t = np.concatenate([np.linspace(0.0, 1.0, k + 1) for k in m.tolist()])
    pts = a[idx] + (b[idx] - a[idx]) * t[:, None]
    return np.unique(_flat(np.floor((pts - grid.origin) / grid.h).astype(np.int64), np.asarray(grid.shape)))


#: A surface with more triangles than this is not mapped where the map is a reading for the stage
#: (the check's time box): the hole finder's reading stands, and the record says so.
MAP_MAX_TRIANGLES = 2_000_000
#: The leak test's grid (INS-5): a staged boundary is closed already, so a coarser grid will do.
LEAK_CELLS = 2_000_000
#: The grid's cube count follows the surface's detail: this many cubes a triangle, never fewer
#: than MIN_GRID_CELLS nor more than GRID_CELLS.
CELLS_PER_TRIANGLE = 400
MIN_GRID_CELLS = 1_500_000


def read_inside_or_none(mesh, *, cells: int = GRID_CELLS, why: list | None = None,
                        close_up_to: float | None = None) -> InsideMap | None:
    """The map, or None when the surface is too big to map in the check's time or cannot be read:
    never a reason to fail the caller (the old readings stand). `close_up_to` (the part's own
    length unit) reads it with every leak up to that wide closed (INS-8)."""
    import logging
    try:
        if mesh is None or len(mesh.faces) > MAP_MAX_TRIANGLES:
            if why is not None:
                why.append("the surface was too big to map")
            return None
        # the grid as fine as the tessellation's own detail asks, up to the budget: a coarse
        # surface of a few thousand triangles has no loop a finer grid would see
        cells = int(min(cells, max(MIN_GRID_CELLS, CELLS_PER_TRIANGLE * len(mesh.faces))))
        if close_up_to:
            h = Grid.around(mesh.verts.min(axis=0), mesh.verts.max(axis=0), cells=cells).h
            return read_inside(mesh, cells=cells, close=closing_radius(float(close_up_to), h))
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


# ------------------------------------------------------------------- closing leaks (INS-8) ----
#: The closings tried for a surface with no way through it, in cubes of radius (a closing of
#: radius r closes every gap up to 2r cubes wide): the first that finds the part's inside is read.
LEAK_RADII = (1, 2, 3, 4)
#: A closing the user asked for is never wider than this many cubes of radius on the staging's grid.
LEAK_MAX_RADIUS = 6


#: A body that is the fluid gets its closed shell's inside back from a closing only when the map as
#: drawn read no pocket at all and the inside regained holds at least this share of the part's box:
#: a crevice the closing shut is no body.
BODY_SHARE = 0.05


def _box_volume(mesh) -> float:
    return float(np.prod(np.maximum(mesh.verts.max(axis=0) - mesh.verts.min(axis=0), 0.0)))


def closing_radius(up_to: float, h: float) -> int:
    """The closing's radius in cubes of `h` that closes every gap up to `up_to` wide."""
    return int(min(max(math.ceil(float(up_to) / (2.0 * float(h)) - 1e-9), 1), LEAK_MAX_RADIUS))


def surface_is_open(mesh) -> bool:
    """Whether a surface can leak at all: an edge not on two faces, or more than one piece. A
    closed surface in one piece keeps its inside and outside apart: it has no leak."""
    if bool(np.any(mesh.count != 2)):
        return True
    return int(_surface_pieces(mesh).max()) > 0


def read_leaks(mesh, *, cells: int = GRID_CELLS, plain: InsideMap | None = None,
               why: list | None = None) -> InsideMap | None:
    """THE LEAKS OF A SURFACE (INS-8): for a surface whose map as drawn finds no way through it and
    that is open (an open edge or a second piece), the map read with its leaks closed - the first
    closing in LEAK_RADII that finds a passage with two mouths or more (or, for a body that is the
    fluid, the inside of its closed shell the map as drawn had none of), with every leak it closed
    narrower than that inside (a gap as wide as the passage is a missing piece, not a leak). None
    when the map as drawn already finds the way through, the surface is closed, or no closing finds
    it: the map as drawn stands."""
    try:
        if mesh is None or len(mesh.faces) > MAP_MAX_TRIANGLES:
            return None
        if plain is not None and plain.has_inside():
            return None
        if not surface_is_open(mesh):
            return None
        cells = int(min(cells, max(MIN_GRID_CELLS, CELLS_PER_TRIANGLE * len(mesh.faces))))
        shelled = plain.enclosed_cells if plain is not None else 0
        for r in LEAK_RADII:
            m = read_inside(mesh, cells=cells, close=r)
            if not m.leaks:
                continue
            if m.has_inside():
                widest = {}
                for p in m.passages():
                    widest[p.id] = 2.0 * p.clearance
                labels_of = {g: pid for pid, gs in m.pocket_labels.items() for g in gs}
                ok = all(s.width < max(widest.get(labels_of.get(g, -1), 0.0) for g in s.passage) for s in m.leaks)
            elif (not m.passages() and not shelled and plain is not None and not plain.pockets
                  and m.enclosed_volume >= BODY_SHARE * _box_volume(mesh)):
                # A BODY THAT IS THE FLUID, its skin holed: the closing gives it back the inside of
                # its closed shell the map as drawn lost
                ok = all(s.width < 2.0 * m.enclosed_clearance for s in m.leaks)
            else:
                continue
            if ok:
                return m
            if why is not None:
                why.append(f"closing {r} cube(s) found a leak as wide as the passage itself: not a leak")
            return None
        return None
    except PartialMap as exc:
        if why is not None:
            why.append(str(exc))
        return None
    except Exception as exc:  # noqa: BLE001 - a reading that fails leaves the map as drawn
        import logging
        logging.getLogger(__name__).warning("leak reading failed (%s: %s)", type(exc).__name__, exc)
        if why is not None:
            why.append(f"the leaks could not be read ({type(exc).__name__})")
        return None


def with_hole_leaks(imap: InsideMap, holes) -> None:
    """A leaky reading's small holes are leaks too (INS-8): every hole of the surface too small for
    the grid to see (the shape rules' `holes`, each with a centroid and an equivalent diameter) that
    no leak already covers is listed as one, its width its own size - the map found the part's
    inside only once gaps of that scale were closed, so a hole of that scale is one of them."""
    if not imap.leaks:
        return
    for h in holes:
        d = float(h.equivalent_diameter)
        if any(imap.leak_distance(s, h.centroid) <= 0.5 * d + 1.5 * imap.h for s in imap.leaks):
            continue
        imap.leaks.append(LeakSpot(id=-1, point=np.asarray(h.centroid, dtype=float), width=d,
                                   cubes=np.zeros(0, dtype=np.int64), kind="hole"))
    imap.leaks.sort(key=lambda s: (-s.width, float(s.point[0]), float(s.point[1]), float(s.point[2])))
    for k, s in enumerate(imap.leaks):
        s.id = k


def proposed_leak_size(imap: InsideMap) -> float:
    """The leak size the stage proposes (INS-8): the widest leak found, and a cube more, at two
    significant figures, rounded up - so every leak listed is closed."""
    if not imap.leaks:
        return 0.0
    x = max(s.width for s in imap.leaks) + imap.h
    e = math.floor(math.log10(x)) - 1
    return math.ceil(x / 10.0 ** e - 1e-9) * 10.0 ** e


def leak_facts(imap: InsideMap, mm_per_unit: float = 1000.0) -> dict:
    """What the check stores and the stage shows about the leaks (INS-8)."""
    return {"rule": "INS-8", "found": len(imap.leaks),
            "close_up_to_mm": round(proposed_leak_size(imap) * mm_per_unit, 4),
            "cell_mm": round(imap.h * mm_per_unit, 4), "radius_cubes": int(imap.close),
            "leaks": [s.as_dict(mm_per_unit) for s in imap.leaks[:MAX_LEAKS_LISTED]],
            "more": max(0, len(imap.leaks) - MAX_LEAKS_LISTED)}


#: The stage lists at most this many leaks, the widest first.
MAX_LEAKS_LISTED = 20


def leak_covers(imap: InsideMap, V: np.ndarray, F: np.ndarray, spots=None) -> np.ndarray:
    """THE LEAK COVERS (INS-8): triangles that close each leak where the passage meets it - the
    faces of its closed cubes the passage touches - with the cover's rim laid onto the surface
    (V, F: the triangles the leak is in), so no gap is left between the cover and the wall, and
    its inside smoothed. Triangles (n, 3, 3), facing out of the passage. Deterministic."""
    shape = np.asarray(imap.shape)
    lab = imap.labels
    if lab is None or not imap.leaks:
        return np.zeros((0, 3, 3))
    out = []
    for s in (imap.leaks if spots is None else spots):
        tris = _cover_of(s, lab, shape, imap.origin, imap.h)
        if not len(tris):
            continue
        out.append(_laid_on(tris, V, F, imap.h))
    return np.concatenate(out) if out else np.zeros((0, 3, 3))


# each of the six directions: (axis, sign) and the four corners of the face between a cube and its
# neighbour that way, as offsets of the cube's low corner, wound to face along (axis, sign)
_FACE: dict[tuple[int, int], np.ndarray] = {}
for _ax in range(3):
    _u, _w = [a for a in range(3) if a != _ax]
    for _sg in (1, -1):
        _base = np.zeros(3, dtype=np.int64)
        if _sg > 0:
            _base[_ax] = 1
        _corners: list[np.ndarray] = []
        for du, dw in ((0, 0), (1, 0), (1, 1), (0, 1)):
            q = _base.copy()
            q[_u] += du
            q[_w] += dw
            _corners.append(q)
        _c = np.asarray(_corners)
        # (u, w, ax) is right-handed for ax = 0 (y, z, x) and 2 (x, y, z); for ax = 1 (x, z, y) it is not
        _right = _ax != 1
        if (_sg > 0) != _right:
            _c = _c[::-1]
        _FACE[(_ax, _sg)] = _c


def _cover_of(s: LeakSpot, lab: np.ndarray, shape: np.ndarray, origin: np.ndarray, h: float) -> np.ndarray:
    """The cube faces between a leak's closed cubes and its passage, as triangles facing out of the
    passage (into the leak)."""
    sx, sy, sz = (int(v) for v in shape)
    c = s.cubes
    i, j, k = c // (sy * sz), (c // sz) % sy, c % sz
    passage = np.asarray(sorted(s.passage), dtype=np.int64)
    quads = []
    for ax in range(3):
        for sg in (1, -1):
            d = np.zeros(3, dtype=np.int64)
            d[ax] = sg
            ii, jj, kk = i - d[0], j - d[1], k - d[2]       # the passage cube behind this face
            ok = (ii >= 0) & (ii < sx) & (jj >= 0) & (jj < sy) & (kk >= 0) & (kk < sz)
            nb = np.zeros(len(c), dtype=np.int64)
            nb[ok] = lab[(ii[ok] * sy + jj[ok]) * sz + kk[ok]]
            hit = ok & np.isin(nb, passage)
            if not hit.any():
                continue
            # the face is the passage cube's face toward the leak: (axis, sign) out of the passage
            low = np.stack([ii[hit], jj[hit], kk[hit]], axis=1)
            quads.append(low[:, None, :] + _FACE[(ax, sg)][None, :, :])
    if not quads:
        return np.zeros((0, 3, 3))
    Q = np.concatenate(quads).astype(np.float64) * h + origin
    return np.concatenate([Q[:, [0, 1, 2]], Q[:, [0, 2, 3]]])


def _laid_on(T: np.ndarray, V: np.ndarray, F: np.ndarray, h: float) -> np.ndarray:
    """A cover's rim moved onto the nearest point of the surface (V, F), and its inside points
    smoothed toward their neighbours (the rim held): a cover that meets the wall, not a staircase
    a cube off it."""
    from scipy.spatial import cKDTree
    P = T.reshape(-1, 3)
    key = np.round((P - P.min(axis=0)) / h * 2.0).astype(np.int64)
    uq, inv = np.unique(key, axis=0, return_inverse=True)
    inv = inv.reshape(-1)
    pts = np.zeros((len(uq), 3))
    np.add.at(pts, inv, P)
    pts /= np.bincount(inv, minlength=len(uq))[:, None]
    tri = inv.reshape(-1, 3)
    # the rim: edges on one triangle of the cover
    e = np.sort(np.concatenate([tri[:, [0, 1]], tri[:, [1, 2]], tri[:, [2, 0]]]), axis=1)
    ue, cnt = np.unique(e, axis=0, return_counts=True)
    rim = np.zeros(len(pts), dtype=bool)
    rim[ue[cnt == 1].ravel()] = True
    # the surface near the cover, by its triangles' boxes
    lo, hi = pts.min(axis=0) - 3.0 * h, pts.max(axis=0) + 3.0 * h
    tv = V[F]
    near = np.all(tv.max(axis=1) >= lo, axis=1) & np.all(tv.min(axis=1) <= hi, axis=1)
    cand = tv[near]
    if len(cand) and rim.any():
        idx = np.flatnonzero(rim)
        best = np.full(len(idx), np.inf)
        to = pts[idx].copy()
        tree = cKDTree(cand.mean(axis=1))
        k = min(len(cand), 48)
        _d, nn = tree.query(pts[idx], k=k)
        nn = np.atleast_2d(nn).reshape(len(idx), -1)
        for col in range(nn.shape[1]):
            q = _closest_on_triangles(pts[idx], cand[nn[:, col]])
            d = np.linalg.norm(q - pts[idx], axis=1)
            better = d < best
            best[better] = d[better]
            to[better] = q[better]
        move = best <= 2.5 * h
        pts[idx[move]] = to[move]
    # the inside points smoothed (the rim held), a few deterministic sweeps
    nbrs = np.concatenate([tri[:, [0, 1]], tri[:, [1, 2]], tri[:, [2, 0]], tri[:, [1, 0]], tri[:, [2, 1]], tri[:, [0, 2]]])
    free = ~rim
    for _ in range(4):
        acc = np.zeros_like(pts)
        np.add.at(acc, nbrs[:, 0], pts[nbrs[:, 1]])
        deg = np.bincount(nbrs[:, 0], minlength=len(pts))[:, None]
        avg = acc / np.maximum(deg, 1)
        pts[free] = 0.5 * pts[free] + 0.5 * avg[free]
    out = pts[tri]
    area = np.linalg.norm(np.cross(out[:, 1] - out[:, 0], out[:, 2] - out[:, 0]), axis=1)
    return out[area > 1e-6 * h * h]


def _closest_on_triangles(p: np.ndarray, T: np.ndarray) -> np.ndarray:
    """The closest point to each p on its triangle T (Ericson, Real-Time Collision Detection 5.1.5)."""
    a, b, c = T[:, 0], T[:, 1], T[:, 2]
    ab, ac, ap = b - a, c - a, p - a
    d1, d2 = np.einsum("ij,ij->i", ab, ap), np.einsum("ij,ij->i", ac, ap)
    bp = p - b
    d3, d4 = np.einsum("ij,ij->i", ab, bp), np.einsum("ij,ij->i", ac, bp)
    cp = p - c
    d5, d6 = np.einsum("ij,ij->i", ab, cp), np.einsum("ij,ij->i", ac, cp)
    va = d3 * d6 - d5 * d4
    vb = d5 * d2 - d1 * d6
    vc = d1 * d4 - d3 * d2
    out = np.empty_like(p)
    done = np.zeros(len(p), dtype=bool)

    def put(mask, val):
        m = mask & ~done
        out[m] = val[m]
        done[m] = True

    put((d1 <= 0) & (d2 <= 0), a)
    put((d3 >= 0) & (d4 <= d3), b)
    with np.errstate(divide="ignore", invalid="ignore"):
        v = d1 / (d1 - d3)
        put((vc <= 0) & (d1 >= 0) & (d3 <= 0), a + v[:, None] * ab)
        put((d6 >= 0) & (d5 <= d6), c)
        w = d2 / (d2 - d6)
        put((vb <= 0) & (d2 >= 0) & (d6 <= 0), a + w[:, None] * ac)
        w2 = (d4 - d3) / ((d4 - d3) + (d5 - d6))
        put((va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0), b + w2[:, None] * (c - b))
        den = 1.0 / (va + vb + vc)
        v2, w3 = vb * den, vc * den
        put(np.ones(len(p), dtype=bool), a + ab * v2[:, None] + ac * w3[:, None])
    return np.where(np.isfinite(out), out, a)


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
