# Responsibility: THE CFD WRAP - turn a surface that no flow engine could mesh (faces that cross,
# gaps, open shells, non-manifold edges, doubled surfaces) into a clean skin: closed, manifold,
# every face the right way out, within a measured distance of the original, with every face named
# after the nearest patch of the original.
# Owns: the wrap grid, the wrap distance, the flood of the fluid side, the skin drawn from it, the
# naming of the skin's faces, and the measured deviation from the original with the bridged spots.
# Boundaries: numpy, scipy.ndimage and VTK over triangles in metres. It never touches the
# customer's file and never runs a mesher: the run's staging calls it (pipeline/cfd_wrap.py says
# when) and meshes what it returns. No OpenCASCADE, no storage, no model, never a file name.
# Collaborates with: pipeline/cfd_wrap.py (when the rung runs, the words), cad/internal_surface.py
# and cad/cad_tessellate.py (the staged fluid boundary it wraps for internal flow),
# agents/builder/attempt.py (the body it wraps for external flow), cad/inside_space.py (the grid's
# conservative raster and the exact point-to-triangle distance).
"""THE WRAP, IN ONE PARAGRAPH (docs/RULES.md LAD-cfd-5..7).

The surface is drawn into a grid of cubes. Every cube centre gets its distance to the surface
(exact near the surface). The FREE space is every point farther than the wrap distance r from the
surface; the FLUID SIDE is the free space the flow reaches - the part joined to the grid's border
(flow around a body) or to the point inside the flow (flow through a part, whose openings are the
staged lids). The wrapped fluid is that free space grown back by r: every point within r of it.
Its boundary is the skin. Where the surface was sound the skin lies on it (the field there is the
exact distance, so the skin follows it to well under a cube); where it had a gap narrower than 2r
- a crack between faces, a seam left open, a hole in a shell - the skin spans it: a BRIDGED spot,
measured and named. Faces that cross, doubled surfaces and the inside of overlapping shells are
never reached by the flood, so they simply are not part of the skin. A sheet with flow on both
sides (a zero-thickness fin) is kept as a slab about one cube thick.

The skin is then named face by face after the nearest face of the original, and measured against
it both ways: how far the skin lies from the original (its largest and 95th-percentile distance,
and the spots where it bridged), and how far the wetted original lies from the skin (a feature
smaller than a cube the skin smoothed over).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

#: The most cubes one wrap grid holds (about 1.5 GB of working arrays at the peak). A finer wrap
#: than this allows is held to it, and the wrap distance grows with the cube (WRAP_CUBES_PER_R).
WRAP_GRID_CUBES = 32_000_000
#: The wrap distance spans at least this many cubes: the skin is drawn from the exact distance, and
#: below 2.5 cubes the grid cannot tell a gap of 2r from a wall.
WRAP_CUBES_PER_R = 2.5
#: The wrap distance from the part's own size: the diagonal of the surface over this - about half
#: the wall cell a standard plan puts on a part (a gap narrower than a wall cell or so is closed).
WRAP_DIAG_SHARE = 500.0
#: ...and, for flow through a part, never more than this share of the narrowest way the flow takes
#: from the point inside the flow to every opening (the bottleneck's half-width): a gap of 2r is
#: closed, so a real passage keeps at least (1 - 2 x 0.15) = 70% of its width open.
WRAP_PASSAGE_SHARE = 0.15
#: The flood must still reach every opening; when it does not at this distance, the distance is cut
#: by this factor and the wrap is drawn again, at most WRAP_RETRIES times.
WRAP_SHRINK = 0.6
WRAP_RETRIES = 3
#: Flow round a body must stay outside it. The space a wrap of INSIDE_PROBE times the distance seals
#: off from the open air, that the flow at the distance still reaches, is the body's inside reached
#: through a gap; when it is more than INSIDE_REACHED_SHARE of the body at that wider wrap, the wrap
#: is drawn again closing gaps twice as wide, never wider than the diagonal over WRAP_MAX_DIAG_SHARE.
#: A sheet the flow wets on both sides seals nothing off, so it is never taken for a hollow body.
INSIDE_PROBE = 4.0
INSIDE_REACHED_SHARE = 0.25
WRAP_MAX_DIAG_SHARE = 60.0
#: A point of the skin farther than this many cubes from the original lies across a gap it bridged.
BRIDGE_CUBES = 1.5
#: A piece of the skin enclosing fewer than this many cubes is grid noise, not a body, and is dropped.
MIN_PIECE_CUBES = 8
#: The spots listed by name and place (the widest first); the rest are counted.
MAX_SPOTS = 20
#: The skin is coarsened (VTK's quadric decimation, volume kept) to about the triangles of edge
#: SKIN_EDGE_SHARE x r its area holds - the wrap cannot place a detail finer than r anyway - never fewer than
#: MIN_SKIN_TRIANGLES, never more than MAX_SKIN_TRIANGLES; a coarsening that would leave it open is
#: not kept. The drawn skin has a few triangles per grid cube, so it is several times finer.
MAX_SKIN_TRIANGLES = 600_000
MIN_SKIN_TRIANGLES = 20_000
SKIN_EDGE_SHARE = 0.7

#: Where the wrap's own records live, beside the run's surface (the run's workspace).
WRAP_REQUEST_FILE = "cfd_wrap.json"
WRAP_REPORT_FILE = "cfd_wrap_report.json"

RULE_RUNG = "LAD-cfd-5"
RULE_DISTANCE = "LAD-cfd-6"
RULE_REPORT = "LAD-cfd-7"


class WrapError(RuntimeError):
    """The wrap could not make a skin the flow can use (the words say why); the run then ends on
    the failure it already had."""


@dataclass
class WrapResult:
    verts: np.ndarray                 # (n, 3) metres
    faces: np.ndarray                 # (m, 3) indices into verts, outward from the fluid
    labels: np.ndarray                # (m,) patch name per face
    report: dict = field(default_factory=dict)
    #: the fluid side's deepest point (farthest from every wall: well inside the wrap), and
    #: whether the given point inside the flow lies on the fluid side the wrap was drawn from
    inside: np.ndarray | None = None
    seed_in_fluid: bool = False


# ------------------------------------------------------------------- the distance to a surface --
class SurfaceDistance:
    """The exact distance from points to a fixed set of triangles. The triangles are sampled
    (every corner, every centre, and points no farther apart than `spacing` over a long one); a
    point's candidates are the triangles of its nearest samples, and its distance is the exact one
    to the nearest of them (inside_space._closest_on_triangles)."""

    def __init__(self, T: np.ndarray, spacing: float, *, k: int = 8) -> None:
        from scipy.spatial import cKDTree
        self.T = np.asarray(T, dtype=np.float64).reshape(-1, 3, 3)
        pts, owner = _samples(self.T, spacing)
        self.owner = owner
        self.tree = cKDTree(pts)
        self.k = int(max(1, min(k, len(pts))))

    def query(self, P: np.ndarray, *, chunk: int = 100_000) -> tuple[np.ndarray, np.ndarray]:
        """(distance, nearest triangle index) for every point of P (n, 3). Chunks run on the
        machine's threads (numpy and the tree release the interpreter); each point's answer is the
        same whatever the split, so the result is identical on any number of threads."""
        from concurrent.futures import ThreadPoolExecutor

        from meshpipeline.cad.inside_space import _closest_on_triangles
        P = np.asarray(P, dtype=np.float64).reshape(-1, 3)
        dist = np.full(len(P), np.inf)
        tri = np.zeros(len(P), dtype=np.int64)

        def one(s: int) -> None:
            p = P[s:s + chunk]
            _d, nn = self.tree.query(p, k=self.k)
            nn = np.asarray(nn).reshape(len(p), -1)
            best = np.full(len(p), np.inf)
            arg = np.zeros(len(p), dtype=np.int64)
            for col in range(nn.shape[1]):
                t = self.owner[nn[:, col]]
                q = _closest_on_triangles(p, self.T[t])
                d = np.linalg.norm(q - p, axis=1)
                better = d < best
                best[better] = d[better]
                arg[better] = t[better]
            dist[s:s + chunk] = best
            tri[s:s + chunk] = arg

        starts = list(range(0, len(P), chunk))
        if len(starts) <= 1:
            for s in starts:
                one(s)
        else:
            with ThreadPoolExecutor(max_workers=_threads()) as pool:
                list(pool.map(one, starts))
        return dist, tri


def _threads() -> int:
    """The threads the wrap's distance queries use: the machine's cores, at most 8 (the answer does
    not depend on it)."""
    import os
    return max(1, min(8, os.cpu_count() or 1))


def _samples(T: np.ndarray, spacing: float) -> tuple[np.ndarray, np.ndarray]:
    """Points on every triangle, with the triangle each belongs to, so that every point of every
    triangle lies within about `spacing` of one of its own samples: the corners and the centre;
    points no farther apart than `spacing` along each side (a long sliver - a CAD face's fan
    triangle - is covered by its sides, every point of it being within its width of one); and a
    barycentric lattice over a triangle wider than `spacing`. Deterministic."""
    n = len(T)
    sp = max(float(spacing), 1e-30)
    pts = [T.reshape(-1, 3), T.mean(axis=1)]
    owner = [np.repeat(np.arange(n), 3), np.arange(n)]
    for k in range(3):
        a, b = T[:, k], T[:, (k + 1) % 3]
        steps = np.clip(np.ceil(np.linalg.norm(b - a, axis=1) / sp), 1, 20_000).astype(np.int64)
        for st in np.unique(steps[steps > 1]).tolist():
            sel = np.flatnonzero(steps == st)
            w = (np.arange(1, st, dtype=np.float64) / st)[None, :, None]
            pts.append((a[sel][:, None, :] + w * (b[sel] - a[sel])[:, None, :]).reshape(-1, 3))
            owner.append(np.repeat(sel, st - 1))
    # the lattice: as many rows as the triangle is WIDE (twice its area over its longest side) in
    # samples, never more than 64 - its sides are sampled above
    area2 = np.linalg.norm(np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0]), axis=1)
    edge = np.max(np.linalg.norm(T - np.roll(T, 1, axis=1), axis=2), axis=1)
    width = area2 / np.maximum(edge, 1e-300)
    m = np.where(width > sp, np.clip(np.ceil(edge / sp), 1, 64), 1).astype(np.int64)
    for mm in np.unique(m[m > 1]).tolist():
        sel = np.flatnonzero(m == mm)
        ij = np.array([(i, j) for i in range(mm + 1) for j in range(mm + 1 - i)], dtype=np.float64) / mm
        a = T[sel, 0][:, None, :]
        ab = (T[sel, 1] - T[sel, 0])[:, None, :]
        ac = (T[sel, 2] - T[sel, 0])[:, None, :]
        lat = a + ij[None, :, 0:1] * ab + ij[None, :, 1:2] * ac
        pts.append(lat.reshape(-1, 3))
        owner.append(np.repeat(sel, len(ij)))
    return np.concatenate(pts), np.concatenate(owner)


# ------------------------------------------------------------------------------- the grid ----
@dataclass
class WrapGrid:
    origin: np.ndarray                # the centre of cube (0, 0, 0)
    h: float
    shape: tuple[int, int, int]

    @property
    def n(self) -> int:
        return int(np.prod(self.shape))

    def centres(self, flat: np.ndarray) -> np.ndarray:
        ijk = np.stack(np.unravel_index(flat, self.shape), axis=1).astype(np.float64)
        return self.origin + ijk * self.h

    def index_of(self, P: np.ndarray) -> np.ndarray:
        q = np.rint((np.asarray(P, dtype=np.float64).reshape(-1, 3) - self.origin) / self.h)
        q = np.clip(q.astype(np.int64), 0, np.asarray(self.shape) - 1)
        return np.ravel_multi_index(q.T, self.shape)


def grid_for(lo, hi, r: float, *, cubes: int = WRAP_GRID_CUBES) -> tuple[WrapGrid, float]:
    """The wrap's grid round the box lo..hi for a wrap distance r: cubes of r / 2.5, a border of
    r + 4 cubes all round, never more than `cubes` cubes. Returns (grid, r) - r grown with the
    cube when the cap holds the grid coarser than r asks for."""
    lo, hi = np.asarray(lo, dtype=np.float64), np.asarray(hi, dtype=np.float64)
    size = np.maximum(hi - lo, 1e-12)
    h = float(r) / WRAP_CUBES_PER_R
    for _ in range(12):
        pad = r + 4.0 * h
        n = np.ceil((size + 2.0 * pad) / h) + 1
        if float(np.prod(n)) <= cubes:
            break
        h *= (float(np.prod(n)) / cubes) ** (1.0 / 3.0) * 1.01
        r = max(r, WRAP_CUBES_PER_R * h)
    pad = r + 4.0 * h
    shape = tuple(int(v) for v in np.ceil((size + 2.0 * pad) / h) + 1)
    return WrapGrid(origin=lo - pad, h=h, shape=shape), float(r)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- the distance ----
def wrap_distance(diag: float, *, bottleneck: float | None = None) -> float:
    """THE WRAP DISTANCE (LAD-cfd-6), from the geometry alone: the surface's diagonal over
    WRAP_DIAG_SHARE (about half a standard wall cell), and - for flow through a part - never more
    than WRAP_PASSAGE_SHARE of the narrowest way's half-width between the point inside the flow and
    every opening, so no real passage is closed."""
    r = float(diag) / WRAP_DIAG_SHARE
    if bottleneck is not None and bottleneck > 0:
        r = min(r, WRAP_PASSAGE_SHARE * float(bottleneck))
    return r


# ------------------------------------------------------------------------------ the wrap ----
def wrap_surface(T: np.ndarray, labels: np.ndarray, *, flow: str, seed=None,
                 ports: tuple[str, ...] = (), distance: float | None = None,
                 cubes: int | None = None) -> WrapResult:
    """Wrap the triangles T (n, 3, 3, metres), each named by `labels`, for `flow` ("external": the
    flow is round the body, reached from the grid's border; "internal": the flow is inside a
    closed staged boundary, reached from `seed`, with `ports` the labels of its lids, every one of
    which the flow must still reach). `distance` overrides the rule (tests); None = LAD-cfd-6."""
    T = np.asarray(T, dtype=np.float64).reshape(-1, 3, 3)
    labels = np.asarray(labels, dtype=object).reshape(-1)
    if len(T) < 4 or len(labels) != len(T):
        raise WrapError("the surface holds too few triangles to wrap")
    internal = flow == "internal"
    cubes = int(cubes or WRAP_GRID_CUBES)
    if internal and seed is None:
        raise WrapError("flow through a part needs a point inside the flow to wrap from")
    lo, hi = T.reshape(-1, 3).min(axis=0), T.reshape(-1, 3).max(axis=0)
    diag = float(np.linalg.norm(hi - lo))
    r = float(distance) if distance else wrap_distance(diag)
    how = "rule" if not distance else "given"
    tries: list[dict] = []
    bottleneck = None
    for attempt in range(WRAP_RETRIES + 1):
        grid, r = grid_for(lo, hi, r, cubes=cubes)
        field_ = _Field(T, grid)
        if internal and attempt == 0 and not distance:
            # the narrowest way the flow takes to every opening, read on this first grid
            bottleneck = field_.bottleneck(seed, labels, ports)
            if bottleneck is not None:
                r_pass = wrap_distance(diag, bottleneck=bottleneck)
                if r_pass < r * 0.999:
                    r = r_pass
                    grid, r = grid_for(lo, hi, r, cubes=cubes)
                    field_ = _Field(T, grid)
        reached, why = field_.flood(r, seed=seed if internal else None, labels=labels, ports=ports)
        row = {"distance_m": r, "cube_m": grid.h, "cubes": grid.n, "reached": reached,
               **({"why": why} if why else {})}
        tries.append(row)
        if reached and not internal:
            # FLOW ROUND A BODY MUST STAY OUTSIDE IT: when the flood went in through a gap wider
            # than 2r, the body's own skin is wetted on both sides. Most of it wetted so is a body
            # whose inside the flow reached: the wrap closes twice as wide a gap and tries again
            share = field_.inside_reached_share(r, min(INSIDE_PROBE * r, diag / WRAP_MAX_DIAG_SHARE))
            row["inside_reached_share"] = round(share, 4)
            if share > INSIDE_REACHED_SHARE and attempt < WRAP_RETRIES \
                    and 2.0 * r <= diag / WRAP_MAX_DIAG_SHARE:
                row["why"] = (f"the flow reached the inside of the body ({share:.0%} of it, at a "
                              "wider wrap)")
                r *= 2.0
                continue
            if share > INSIDE_REACHED_SHARE:
                raise WrapError(f"the flow reaches the inside of the body through an opening wider "
                                f"than {2000.0 * r:.3g} mm ({share:.0%} of the body), so a wrap would "
                                "mesh the body hollow")
        if reached:
            break
        if not internal or attempt == WRAP_RETRIES or (
                r * WRAP_SHRINK < WRAP_CUBES_PER_R * grid.h * 0.99 and grid.n >= cubes * 0.9):
            raise WrapError(why or "the wrap could not keep every opening reachable")
        r *= WRAP_SHRINK
    verts, faces = field_.skin(r)
    verts, faces = _oriented(verts, faces, field_, internal=internal)
    target = _skin_target(verts, faces, r)
    if len(faces) > target * 1.2:
        v2, f2 = _decimate(verts, faces, target)
        try:
            _check_closed(v2, f2)
            verts, faces = v2, f2
        except WrapError:
            logger.info("cfd wrap: the coarsened skin was not closed - the full skin is kept")
    _check_closed(verts, faces)
    named = _names(verts, faces, field_, labels)
    if internal:
        missing = [p for p in ports if not np.any(named == p)]
        if missing:
            raise WrapError("the wrapped surface lost the opening(s) " + ", ".join(missing))
    report = _measure(verts, faces, named, field_, r, labels)
    report.update({"distance_rule": RULE_DISTANCE, "flow": flow, "distance_m": r, "distance_from": how,
                   "cube_m": grid.h, "grid": list(grid.shape), "tries": tries,
                   "diag_m": diag,
                   **({"bottleneck_m": bottleneck} if bottleneck is not None else {})})
    import hashlib
    report["skin_sha256"] = hashlib.sha256(np.ascontiguousarray(verts, dtype=np.float64).tobytes()
                                           + np.ascontiguousarray(faces, dtype=np.int64).tobytes()).hexdigest()
    inside, seed_in = field_.inside_points(seed) if internal else (None, False)
    return WrapResult(verts=verts, faces=faces, labels=named, report=report, inside=inside,
                      seed_in_fluid=seed_in)


class _Field:
    """The grid, the distance of every cube centre to the surface, the fluid side and the skin's
    field. One per (surface, grid)."""

    def __init__(self, T: np.ndarray, grid: WrapGrid) -> None:
        from scipy import ndimage

        from meshpipeline.cad.inside_space import Grid, raster
        self.T, self.grid = T, grid
        h = grid.h
        # the cubes the surface passes through (conservative: no path crosses it unmarked); the
        # raster is in cube-box terms, so a box grid whose cube CENTRES are ours is half a cube off
        box = Grid(origin=grid.origin - 0.5 * h, h=h, shape=grid.shape)
        wall = np.zeros(grid.n, dtype=bool)
        wall[raster(T, box)] = True
        self.wall = wall.reshape(grid.shape)
        # the distance of every centre: from the wall's cubes (EDT) away from the surface, and
        # EXACT within the band the wrap reads (computed once, for the widest r it may use)
        approx = ndimage.distance_transform_edt(~self.wall).astype(np.float32) * np.float32(h)
        self.approx = approx
        self.sd = SurfaceDistance(T, spacing=h)
        self.d = approx.copy()
        self.near_tri = np.full(grid.shape, -1, dtype=np.int64)
        self._band_m = 0.0

    def exact_band(self, upto: float) -> None:
        """Exact distances for every centre the wall's EDT puts within `upto` (+ 2 cubes)."""
        if upto <= self._band_m:
            return
        h = self.grid.h
        sel = np.flatnonzero((self.approx.reshape(-1) <= upto + 2.0 * h)
                             & (self.approx.reshape(-1) > self._band_m + 2.0 * h
                                if self._band_m > 0 else True))
        if len(sel):
            d, t = self.sd.query(self.grid.centres(sel))
            self.d.reshape(-1)[sel] = d.astype(np.float32)
            self.near_tri.reshape(-1)[sel] = t
        self._band_m = upto

    def _probes(self, labels, port: str, off: float) -> np.ndarray:
        """Points just off each face of `port`, both sides, `off` metres away."""
        T = self.T[labels == port]
        c = T.mean(axis=1)
        n = np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0])
        nl = np.linalg.norm(n, axis=1)
        ok = nl > 0
        n = n[ok] / nl[ok][:, None]
        c = c[ok]
        P = np.concatenate([c + off * n, c - off * n])
        if len(P) > 4000:
            P = P[np.linspace(0, len(P) - 1, 4000).astype(np.int64)]
        return P

    def _component(self, free: np.ndarray, seed) -> tuple[np.ndarray | None, str]:
        from scipy import ndimage
        lab, _n = ndimage.label(free)
        if seed is None:
            border = np.unique(np.concatenate([
                lab[0].ravel(), lab[-1].ravel(), lab[:, 0].ravel(), lab[:, -1].ravel(),
                lab[:, :, 0].ravel(), lab[:, :, -1].ravel()]))
            border = border[border > 0]
            if not len(border):
                return None, "the flow round the body found no free space at the grid's border"
            return np.isin(lab, border), ""
        q = int(self.grid.index_of(np.asarray(seed, dtype=np.float64))[0])
        k = int(lab.reshape(-1)[q])
        if k == 0:
            # the point inside the flow is within the wrap distance of a wall: the free cube
            # nearest it (by the free space's own distance map) stands in for it
            _dt, idx = ndimage.distance_transform_edt(~free, return_indices=True)
            ijk = np.unravel_index(q, self.grid.shape)
            near = tuple(int(idx[a][ijk]) for a in range(3))
            k = int(lab[near])
            if k == 0:
                return None, "no free space round the point inside the flow"
        comp = lab == k
        if (comp[0].any() or comp[-1].any() or comp[:, 0].any() or comp[:, -1].any()
                or comp[:, :, 0].any() or comp[:, :, -1].any()):
            return None, ("the flow inside the part reaches the open air: the staged fluid boundary "
                          "is open wider than the wrap closes")
        return comp, ""

    def bottleneck(self, seed, labels, ports) -> float | None:
        """The narrowest way's half-width: the largest clearance t at which the free space round
        the point inside the flow still reaches every opening (the widest path's clearance), read
        by halving between one cube and the deepest point. None when it cannot be read."""
        if not ports:
            return None
        h = self.grid.h
        self.exact_band(1.5 * h)
        top = float(self.approx.max())
        lo_t, hi_t = 1.5 * h, top
        if not self._reaches(lo_t, seed, labels, ports):
            return None
        for _ in range(9):
            # past the exact band the wall EDT stands in (within a cube: plenty for a width)
            mid = 0.5 * (lo_t + hi_t)
            if self._reaches(mid, seed, labels, ports):
                lo_t = mid
            else:
                hi_t = mid
            if hi_t - lo_t < h:
                break
        return lo_t

    def _reaches(self, t: float, seed, labels, ports) -> bool:
        comp, _why = self._component(self.d > t, seed)
        if comp is None:
            return False
        flat = comp.reshape(-1)
        return all(flat[self.grid.index_of(self._probes(labels, p, t + 1.5 * self.grid.h))].any()
                   for p in ports)

    def flood(self, r: float, *, seed, labels, ports) -> tuple[bool, str]:
        """The fluid side at wrap distance r: the free space the flow reaches. Internal flow must
        still reach every opening (else the passage would be closed)."""
        self.r = r
        self.exact_band(r)
        comp, why = self._component(self.d > r, seed)
        if comp is None:
            return False, why
        self.fluid = comp
        if seed is not None:
            flat = comp.reshape(-1)
            lost = [p for p in ports
                    if not flat[self.grid.index_of(self._probes(labels, p, r + 1.5 * self.grid.h))].any()]
            if lost:
                return False, (f"at a wrap distance of {r * 1000:.3g} mm the flow no longer reaches "
                               f"the opening(s) {', '.join(lost)}")
        return True, ""

    def skin(self, r: float) -> tuple[np.ndarray, np.ndarray]:
        """The skin: the zero level of the distance to the wrapped fluid's edge. The fluid side's
        own edge lies where the exact distance to the surface is r, so a point's distance to the
        nearest fluid-side cube, less that cube's own distance to the surface beyond r, is its
        distance to that edge; less r, the field is minus the exact distance to the surface on the
        fluid side wherever the surface was sound (the skin lies on it, to well under a cube), and
        positive across every gap the wrap closed. Every sheet the flow wets on both sides is kept
        as a slab about 1.5 cubes thick."""
        from scipy import ndimage
        h = self.grid.h
        dist_f, idx = ndimage.distance_transform_edt(~self.fluid, return_indices=True)
        near = np.ravel_multi_index(tuple(idx), self.grid.shape)
        del idx
        phi = (dist_f.astype(np.float32) * np.float32(h)) - self.d.reshape(-1)[near].reshape(self.grid.shape)
        del dist_f, near
        self.sheets = self._sheet_faces(r)
        if len(self.sheets):
            sd = SurfaceDistance(self.T[self.sheets], spacing=h)
            sel = np.flatnonzero(self.d.reshape(-1) <= 1.0 * h)
            if len(sel):
                ds, _t = sd.query(self.grid.centres(sel))
                flat = phi.reshape(-1)
                flat[sel] = np.maximum(flat[sel], (0.75 * h - ds).astype(np.float32))
        self.phi = phi
        return _contour(phi, self.grid)

    def inside_points(self, seed) -> tuple[np.ndarray, bool]:
        """(the fluid side's deepest cube centre, whether `seed` lies on the fluid side)."""
        from scipy import ndimage
        depth = ndimage.distance_transform_edt(self.fluid)
        q = int(np.argmax(depth))
        deep = self.grid.centres(np.array([q]))[0]
        if seed is None:
            return deep, False
        k = int(self.grid.index_of(np.asarray(seed, dtype=np.float64))[0])
        return deep, bool(self.fluid.reshape(-1)[k])

    def inside_reached_share(self, r: float, R: float) -> float:
        """Of the body as a wrap at R reads it (all the space it does not reach from the border),
        the share that is space the wrap at R seals off but the flow at r reaches: the body's
        inside, reached through gaps between 2r and 2R wide."""
        from scipy import ndimage
        if R <= r:
            return 0.0
        free_R = self.d > R
        lab, _n = ndimage.label(free_R)
        border = np.unique(np.concatenate([
            lab[0].ravel(), lab[-1].ravel(), lab[:, 0].ravel(), lab[:, -1].ravel(),
            lab[:, :, 0].ravel(), lab[:, :, -1].ravel()]))
        outside_R = np.isin(lab, border[border > 0])
        sealed = free_R & ~outside_R & self.fluid
        body_R = int(np.count_nonzero(~outside_R))
        return float(np.count_nonzero(sealed)) / body_R if body_R else 0.0

    def _sheet_faces(self, r: float) -> np.ndarray:
        """The faces the flow wets on BOTH sides: a point just off each side is on the fluid side."""
        T = self.T
        c = T.mean(axis=1)
        n = np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0])
        nl = np.linalg.norm(n, axis=1)
        ok = nl > 0
        n[ok] /= nl[ok][:, None]
        off = r + 1.5 * self.grid.h
        flat = self.fluid.reshape(-1)
        a = flat[self.grid.index_of(c + off * n)]
        b = flat[self.grid.index_of(c - off * n)]
        return np.flatnonzero(ok & a & b)


def _contour(phi: np.ndarray, grid: WrapGrid) -> tuple[np.ndarray, np.ndarray]:
    """The zero level of phi as triangles (VTK's flying edges), points merged."""
    import vtk
    from vtk.util import numpy_support as ns
    img = vtk.vtkImageData()
    nx, ny, nz = grid.shape
    img.SetDimensions(nx, ny, nz)
    img.SetSpacing(grid.h, grid.h, grid.h)
    img.SetOrigin(*[float(v) for v in grid.origin])
    # VTK's point order runs x fastest: the (i, j, k) array is written k-major
    arr = ns.numpy_to_vtk(np.ascontiguousarray(phi.transpose(2, 1, 0)).ravel(), deep=True,
                          array_type=vtk.VTK_FLOAT)
    arr.SetName("phi")
    img.GetPointData().SetScalars(arr)
    fe = vtk.vtkFlyingEdges3D()
    fe.SetInputData(img)
    fe.SetValue(0, 0.0)
    fe.ComputeNormalsOff()
    fe.ComputeGradientsOff()
    fe.ComputeScalarsOff()
    fe.Update()
    clean = vtk.vtkCleanPolyData()
    clean.SetInputConnection(fe.GetOutputPort())
    clean.PointMergingOn()
    clean.SetTolerance(0.0)
    clean.Update()
    out = clean.GetOutput()
    V = ns.vtk_to_numpy(out.GetPoints().GetData()).astype(np.float64) if out.GetNumberOfPoints() else np.zeros((0, 3))
    polys = ns.vtk_to_numpy(out.GetPolys().GetConnectivityArray()) if out.GetNumberOfPolys() else np.zeros(0, dtype=np.int64)
    F = np.asarray(polys, dtype=np.int64).reshape(-1, 3)
    if not len(F):
        raise WrapError("the wrap found no surface between the flow and the part")
    # drop slivers of no area (two corners on one grid edge)
    a = np.linalg.norm(np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]]), axis=1)
    F = F[a > (grid.h * 1e-6) ** 2]
    return V, F


def _pieces(F: np.ndarray, nv: int) -> np.ndarray:
    """The connected piece of every face (shared corners join faces)."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    m = len(F)
    rows = np.repeat(np.arange(m), 3)
    g = coo_matrix((np.ones(3 * m), (rows, F.reshape(-1))), shape=(m, nv)).tocsr()
    adj = g @ g.T
    _n, lab = connected_components(adj, directed=False)
    return lab


def _oriented(V: np.ndarray, F: np.ndarray, fld: _Field, *, internal: bool) -> tuple[np.ndarray, np.ndarray]:
    """Every piece of the skin turned so its faces point AWAY FROM THE FLUID (out of the fluid for
    flow through a part; out of the body, into the flow, for flow round it - the side phi is
    negative on is the fluid's), and the pieces that enclose less than MIN_PIECE_CUBES cubes
    dropped as grid noise."""
    from scipy import ndimage
    lab = _pieces(F, len(V))
    h = fld.grid.h
    keep = np.zeros(len(F), dtype=bool)
    flip = np.zeros(len(F), dtype=bool)
    a, b, c = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
    cen = (a + b + c) / 3.0
    nrm = np.cross(b - a, c - a)
    area2 = np.linalg.norm(nrm, axis=1)
    vol6 = np.einsum("ij,ij->i", a, np.cross(b, c))
    for k in np.unique(lab).tolist():
        sel = lab == k
        if abs(float(vol6[sel].sum())) / 6.0 < MIN_PIECE_CUBES * h ** 3:
            continue
        keep[sel] = True
        idx = np.flatnonzero(sel)
        big = idx[np.argsort(-area2[idx], kind="stable")[:64]]
        u = nrm[big] / np.maximum(area2[big], 1e-300)[:, None]
        fwd = ((cen[big] + 0.75 * h * u) - fld.grid.origin) / h
        back = ((cen[big] - 0.75 * h * u) - fld.grid.origin) / h
        pf = ndimage.map_coordinates(fld.phi, fwd.T, order=1, mode="nearest")
        pb = ndimage.map_coordinates(fld.phi, back.T, order=1, mode="nearest")
        # flow through a part: the normal points toward rising phi, out of the fluid; flow round a
        # body: toward falling phi, out of the body into the flow (an STL body's own convention)
        rising = int(np.sum(pf > pb)) * 2 >= len(big)
        if rising != internal:
            flip[sel] = True
    F = F[keep].copy()
    fl = flip[keep]
    F[fl] = F[fl][:, ::-1]
    used = np.unique(F)
    remap = np.full(len(V), -1, dtype=np.int64)
    remap[used] = np.arange(len(used))
    return V[used], remap[F]


def _skin_target(V: np.ndarray, F: np.ndarray, r: float) -> int:
    """About the triangles of edge r the skin's area holds (an equilateral triangle of edge r is
    sqrt(3)/4 r^2), within MIN_SKIN_TRIANGLES..MAX_SKIN_TRIANGLES."""
    area = float(np.linalg.norm(np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]]), axis=1).sum()) / 2.0
    n = int(area / (np.sqrt(3.0) / 4.0 * (SKIN_EDGE_SHARE * r) ** 2)) if r > 0 else MAX_SKIN_TRIANGLES
    return max(MIN_SKIN_TRIANGLES, min(MAX_SKIN_TRIANGLES, n))


def _decimate(V: np.ndarray, F: np.ndarray, target: int) -> tuple[np.ndarray, np.ndarray]:
    import vtk
    from vtk.util import numpy_support as ns
    pd = _polydata(V, F)
    dec = vtk.vtkQuadricDecimation()
    dec.SetInputData(pd)
    dec.SetTargetReduction(max(0.0, min(0.95, 1.0 - target / max(len(F), 1))))
    dec.VolumePreservationOn()
    dec.Update()
    out = dec.GetOutput()
    V2 = ns.vtk_to_numpy(out.GetPoints().GetData()).astype(np.float64)
    F2 = np.asarray(ns.vtk_to_numpy(out.GetPolys().GetConnectivityArray()), dtype=np.int64).reshape(-1, 3)
    return V2, F2


def _polydata(V: np.ndarray, F: np.ndarray):
    import vtk
    from vtk.util import numpy_support as ns
    pts = vtk.vtkPoints()
    pts.SetData(ns.numpy_to_vtk(np.ascontiguousarray(V), deep=True))
    cells = vtk.vtkCellArray()
    off = np.arange(0, 3 * len(F) + 1, 3, dtype=np.int64)
    cells.SetData(ns.numpy_to_vtkIdTypeArray(off, deep=True),
                  ns.numpy_to_vtkIdTypeArray(np.ascontiguousarray(F.reshape(-1), dtype=np.int64), deep=True))
    pd = vtk.vtkPolyData()
    pd.SetPoints(pts)
    pd.SetPolys(cells)
    return pd


def _edge_keys(F: np.ndarray, *, directed: bool) -> np.ndarray:
    a = np.concatenate([F[:, 0], F[:, 1], F[:, 2]]).astype(np.int64)
    b = np.concatenate([F[:, 1], F[:, 2], F[:, 0]]).astype(np.int64)
    if not directed:
        a, b = np.minimum(a, b), np.maximum(a, b)
    n = int(F.max()) + 1 if len(F) else 1
    return a * n + b


def edge_counts(F: np.ndarray) -> tuple[int, int, int]:
    """(edges on one face, edges on two, edges on three or more), undirected."""
    _u, cnt = np.unique(_edge_keys(F, directed=False), return_counts=True)
    return int(np.sum(cnt == 1)), int(np.sum(cnt == 2)), int(np.sum(cnt > 2))


def _check_closed(V: np.ndarray, F: np.ndarray) -> None:
    """Closed and manifold: every edge on exactly two faces, and the two run it opposite ways."""
    one, _two, many = edge_counts(F)
    if one or many:
        raise WrapError(f"the wrapped surface is not closed ({one} open and {many} shared edges)")
    _u, cnt = np.unique(_edge_keys(F, directed=True), return_counts=True)
    if int(np.sum(cnt > 1)):
        raise WrapError("the wrapped surface's faces do not all turn the same way")


# --------------------------------------------------------------------------- names, measures --
def _names(V: np.ndarray, F: np.ndarray, fld: _Field, labels: np.ndarray) -> np.ndarray:
    """Each face of the skin named after the face of the original nearest its centre."""
    cen = V[F].mean(axis=1)
    _d, t = fld.sd.query(cen)
    return labels[t]


def _measure(V: np.ndarray, F: np.ndarray, named: np.ndarray, fld: _Field, r: float,
             labels: np.ndarray) -> dict:
    """THE DEVIATION (LAD-cfd-7): how far the skin lies from the original, at every corner of the
    skin - the largest and the 95th percentile, over the skin where it lies on the original and
    over all of it - and the BRIDGED spots: connected stretches of skin farther than
    BRIDGE_CUBES cubes from the original, each with its place, size, widest distance and the
    patch it was named after. Then the other way: the wetted original's distance from the skin."""
    h = fld.grid.h
    dv, _t = fld.sd.query(V)
    tol = BRIDGE_CUBES * h
    on = dv <= tol
    out: dict = {"rule": RULE_REPORT, "skin_triangles": int(len(F)), "skin_points": int(len(V)),
                 "max_m": float(dv.max()) if len(dv) else 0.0,
                 "p95_m": float(np.percentile(dv, 95)) if len(dv) else 0.0,
                 "max_on_cad_m": float(dv[on].max()) if on.any() else 0.0,
                 "p95_on_cad_m": float(np.percentile(dv[on], 95)) if on.any() else 0.0,
                 "bridge_threshold_m": float(tol)}
    far = ~on
    spots: list[dict] = []
    if far.any():
        fbad = np.flatnonzero(far[F].any(axis=1))
        sub = F[fbad]
        lab = _pieces(sub, len(V))
        a = np.linalg.norm(np.cross(V[sub[:, 1]] - V[sub[:, 0]], V[sub[:, 2]] - V[sub[:, 0]]), axis=1) / 2.0
        for k in np.unique(lab).tolist():
            sel = lab == k
            pv = np.unique(sub[sel])
            worst = float(dv[pv].max())
            if worst <= tol:
                continue
            names, counts = np.unique(named[fbad[sel]], return_counts=True)
            spots.append({"centre_m": [round(float(x), 6) for x in V[pv].mean(axis=0)],
                          "area_m2": float(a[sel].sum()), "max_m": worst,
                          "patch": str(names[int(np.argmax(counts))])})
        spots.sort(key=lambda s: (-s["max_m"], -s["area_m2"]))
    out["bridged_spots"] = len(spots)
    out["spots"] = spots[:MAX_SPOTS]
    out["bridged_area_m2"] = float(sum(s["area_m2"] for s in spots))
    out["skin_area_m2"] = float(np.linalg.norm(np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]]),
                                               axis=1).sum() / 2.0)
    # the other way: the wetted original (a sample off either side on the fluid side) from the skin
    T = fld.T
    c = T.mean(axis=1)
    n = np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0])
    nl = np.linalg.norm(n, axis=1)
    ok = nl > 0
    n[ok] /= nl[ok][:, None]
    flat = fld.fluid.reshape(-1)
    off = r + 1.5 * h
    wet = ok & (flat[fld.grid.index_of(c + off * n)] | flat[fld.grid.index_of(c - off * n)])
    if wet.any():
        skin = SurfaceDistance(V[F], spacing=h)
        dw, _t2 = skin.query(c[wet])
        out["cad_to_skin_max_m"] = float(dw.max())
        out["cad_to_skin_p95_m"] = float(np.percentile(dw, 95))
        out["smoothed_faces"] = int(np.sum(dw > tol))
    out["wetted_faces"] = int(wet.sum())
    out["dry_faces"] = int(len(T) - wet.sum())
    out["sheet_faces"] = int(len(getattr(fld, "sheets", ())))
    out["patches"] = {str(k): int(v) for k, v in zip(*np.unique(named, return_counts=True))}
    one, two, many = edge_counts(F)
    out["closed"] = one == 0 and many == 0
    out["pieces"] = int(len(np.unique(_pieces(F, len(V)))))
    return out


# ---------------------------------------------------------------------------- the words ----
def _mm(m: float) -> str:
    mm = float(m) * 1000.0
    if mm >= 100:
        return f"{mm:.0f} mm"
    if mm >= 10:
        return f"{mm:.1f} mm"
    return f"{mm:.2g} mm"


def summary_words(report: dict) -> str:
    """The plain sentence: "meshed on a wrapped surface: within 0.4 mm of your CAD everywhere
    except 3 bridged spots" (LAD-cfd-7)."""
    if not report:
        return ""
    within = report.get("max_on_cad_m")
    n = int(report.get("bridged_spots") or 0)
    head = "meshed on a wrapped surface"
    if within is None:
        return head
    text = f"{head}: within {_mm(within)} of your CAD everywhere"
    if n:
        text += (f" except {n} bridged spot{'s' if n != 1 else ''}, where it spans a gap of up to "
                 f"{_mm(2.0 * float(report.get('distance_m') or 0.0))}")
    return text


# ---------------------------------------------------------------------------- the files ----
def read_named_stl(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """(triangles (n, 3, 3), name per triangle) of an STL; a binary or nameless file is one name,
    the file's stem."""
    from meshpipeline.cad.stl_io import read_stl_solids
    solids = read_stl_solids(Path(path))
    tris, names = [], []
    for name, ts in solids.items():
        if not ts:
            continue
        tris.append(np.asarray(ts, dtype=np.float64).reshape(-1, 3, 3))
        names.append(np.full(len(ts), str(name) or Path(path).stem, dtype=object))
    if not tris:
        return np.zeros((0, 3, 3)), np.zeros(0, dtype=object)
    return np.concatenate(tris), np.concatenate(names)


def write_named_stl(path: Path, V: np.ndarray, F: np.ndarray, labels: np.ndarray, *,
                    names: list[str] | None = None, binary_single: bool = False) -> None:
    """The skin as an STL: one ASCII solid per name (in `names` order), or - for a single-solid
    binary original - binary, as the original was."""
    from meshpipeline.cad.stl_io import write_stl_binary
    T = V[F]
    if binary_single:
        write_stl_binary(Path(path), T)
        return
    order = names or sorted({str(x) for x in labels.tolist()})
    with Path(path).open("w", encoding="utf-8") as fh:
        for name in order:
            fh.write(f"solid {name}\n")
            fh.write(_facets_text(T[labels == name]))
            fh.write(f"endsolid {name}\n")


def _facets_text(T: np.ndarray) -> str:
    if not len(T):
        return ""
    v = np.char.mod("%.9g", np.asarray(T, dtype=np.float64).reshape(-1, 3))
    rows = np.char.add(np.char.add(np.char.add("  vertex ", v[:, 0]), np.char.add(" ", v[:, 1])),
                       np.char.add(" ", v[:, 2])).reshape(-1, 3)
    return "".join(f"facet normal 0 0 0\n outer loop\n{a}\n{b}\n{c}\n endloop\nendfacet\n"
                   for a, b, c in rows.tolist())


# ---------------------------------------------------------------------- the run's staging ----
#: The run's surface as drawn, kept beside the wrapped one it is replaced by.
UNWRAPPED_SUFFIX = ".unwrapped.stl"


def _request(workspace: Path) -> dict | None:
    import json
    try:
        rec = json.loads((Path(workspace) / WRAP_REQUEST_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def _write_report(workspace: Path, report: dict) -> None:
    import json
    rep = {**report, "words": summary_words(report)}
    (Path(workspace) / WRAP_REPORT_FILE).write_text(json.dumps(rep, indent=1, default=float),
                                                    encoding="utf-8")


def wrap_body_if_asked(workspace: Path) -> dict | None:
    """FLOW ROUND A BODY, ON THE WRAP RUNG: the run's staged body (input.stl, metres) is replaced by
    its wrap, each solid's name kept; the surface as drawn stays beside it (input.unwrapped.stl).
    Nothing happens without the run's request (pipeline/cfd_wrap.write_request), or for flow through
    a part (its staged fluid boundary is wrapped instead: wrap_staged_if_asked). Returns the
    report, or None when not asked. A wrap that cannot be made raises WrapError."""
    ws = Path(workspace)
    req = _request(ws)
    if not req or req.get("flow") != "external":
        return None
    surface = ws / "input.stl"
    drawn = ws / ("input" + UNWRAPPED_SUFFIX)
    if not drawn.exists():
        if not surface.exists():
            raise WrapError("the run's surface (input.stl) is missing, so there is nothing to wrap")
        drawn.write_bytes(surface.read_bytes())
    from meshpipeline.cad.stl_io import is_ascii_stl
    T, labels = read_named_stl(drawn)
    names = list(dict.fromkeys(labels.tolist()))
    res = wrap_surface(T, labels, flow="external")
    write_named_stl(surface, res.verts, res.faces, res.labels, names=names,
                    binary_single=len(names) == 1 and not is_ascii_stl(drawn))
    _write_report(ws, {**res.report, "route": "body", "source_triangles": int(len(T))})
    logger.info("cfd wrap: the body is wrapped - %s", summary_words(res.report))
    return res.report


def wrap_staged_if_asked(record: dict, out_dir: Path) -> dict:
    """FLOW THROUGH A PART, ON THE WRAP RUNG: the staged fluid boundary (every patch's STL in the
    record the internal staging returns - cad/internal_surface, cad/cad_tessellate) is replaced by
    its wrap, from the record's point inside the flow, every lid still reached; each STL is written
    again in place with the wrapped faces named after it, and the record's openings, box, point
    inside the flow and the one-file boundary follow. The record is returned unchanged when the run
    did not ask (pipeline/cfd_wrap.write_request). A wrap that cannot be made raises WrapError."""
    out_dir = Path(out_dir)
    ws = out_dir.parent
    req = _request(ws)
    if not req or req.get("flow") != "internal" or not isinstance(record, dict):
        return record
    stls = {str(k): Path(v) for k, v in (record.get("stls") or {}).items()}
    if "wall" not in stls:
        raise WrapError("the staged fluid boundary has no wall to wrap")
    parts_T, parts_L = [], []
    for key, path in stls.items():
        tris, _names = read_named_stl(path)
        parts_T.append(tris)
        parts_L.append(np.full(len(tris), key, dtype=object))
    T, labels = np.concatenate(parts_T), np.concatenate(parts_L)
    ports = tuple(k for k in stls if k != "wall")
    seed = record.get("interior_point")
    res = wrap_surface(T, labels, flow="internal", seed=seed, ports=ports)
    from meshpipeline.cad.stl_io import write_stl_binary
    Tw = res.verts[res.faces]
    for key, path in stls.items():
        write_stl_binary(path, Tw[res.labels == key])
    rec = dict(record)
    lo, hi = res.verts.min(axis=0), res.verts.max(axis=0)
    rec["bbox_min"] = [round(float(v), 6) for v in lo]
    rec["bbox_max"] = [round(float(v), 6) for v in hi]
    rec["n_wall_faces"] = int(np.sum(res.labels == "wall"))
    openings = dict(rec.get("openings") or {})
    for key in ports:
        o = dict(openings.get(key) or {})
        lid = Tw[res.labels == key]
        if len(lid):
            nrm = np.cross(lid[:, 1] - lid[:, 0], lid[:, 2] - lid[:, 0])
            area = float(np.linalg.norm(nrm, axis=1).sum() / 2.0)
            cen = (lid.mean(axis=1) * (np.linalg.norm(nrm, axis=1) / 2.0)[:, None]).sum(axis=0) / max(area, 1e-300)
            o["area"] = round(area, 10)
            o["centroid"] = [round(float(v), 6) for v in cen]
            if "flow_area" in o:
                o["flow_area"] = round(area, 10)
        openings[key] = o
    rec["openings"] = openings
    seed_ok = bool(res.seed_in_fluid)
    if not seed_ok and res.inside is not None:
        # the point inside the flow stood within the wrap distance of a wall: the fluid side's
        # deepest point stands in for it (well inside the wrap, by construction)
        rec["interior_point"] = [round(float(v), 7) for v in res.inside]
    fb = rec.get("fluid_boundary")
    if fb:
        wall_name = str(rec.get("wall_name") or "wall")
        named = np.asarray([wall_name if x == "wall" else x for x in res.labels.tolist()], dtype=object)
        write_named_stl(Path(fb), res.verts, res.faces, named,
                        names=[wall_name, *ports])
    facts = dict(rec.get("facts") or {})
    facts["wrap"] = {k: res.report.get(k) for k in ("rule", "distance_m", "cube_m", "max_m",
                                                      "p95_m", "bridged_spots", "closed")}
    rec["facts"] = facts
    rec["wrapped"] = True
    (out_dir / "internal_surface.json").write_text(__import__("json").dumps(rec, indent=1, default=str))
    _write_report(ws, {**res.report, "route": "fluid boundary", "source_triangles": int(len(T)),
                       **({"seed_moved": True} if not seed_ok else {})})
    logger.info("cfd wrap: the fluid boundary is wrapped - %s", summary_words(res.report))
    return rec


__all__ = ["MAX_SPOTS", "RULE_DISTANCE", "RULE_REPORT", "RULE_RUNG", "UNWRAPPED_SUFFIX",
           "WRAP_REPORT_FILE", "WRAP_REQUEST_FILE", "SurfaceDistance", "WrapError", "WrapResult",
           "edge_counts", "grid_for", "read_named_stl", "summary_words", "wrap_body_if_asked",
           "wrap_distance", "wrap_staged_if_asked", "wrap_surface", "write_named_stl"]
