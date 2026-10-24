# Responsibility: Turn an FEA plan's zones into a size field over the part, and measure how much of the part's volume sits at each element size - the histogram the plan's element estimate is read from.
# Boundaries: geometry only, deterministic, no model and no engine. It reads the labelled triangle surface cad/fea_measure measured; it plans nothing (engines/fea_plan.py chooses the sizes).
# Collaborates with: engines/fea_plan.py (zones in, histogram out), engines/fea_driver.py (builds it on the staged part), cad/fea_features.Surface.
"""THE SIZE FIELD AND ITS VOLUME HISTOGRAM.

A plan zone asks for elements of `size_m` on its faces (or along an edge, for a capped sharp
corner), held out to `reach_m` from them, then growing by the plan's growth rate:

    h_zone(x) = size + (growth - 1) * max(0, distance(x, zone) - reach)

and the field is the smallest of every zone's, never coarser than the plan's base size. Every
engine adapter is handed the same zones, so the field is engine-neutral; this module is its one
numeric reading, and the element estimate is read from it before anything is meshed.

THE HISTOGRAM. The part's volume is cut into cubes (an octree), each cube small against the
zone size at its centre, partly-inside cubes counted by the signed distance to the surface. Each
cube's volume is filed under that zone size, so the element count at ANY base size is

    elements(base) = PER_H3 * sum(volume / min(h_zone, base) ** 3)

without measuring again. The histogram is a short list of (size, volume) rows: it is recorded in
the plan, so the plan can be worked out again from what it recorded.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

#: Zones whose sizes are within this ratio share one distance structure (their smallest size and
#: largest reach: never coarser than any of them asked).
GROUP_RATIO = 1.2
#: A cube is a leaf once its edge is at most this share of the zone size at its centre (the field
#: varies by at most growth-1 per unit length, so the size across such a cube moves by ~15%).
LEAF_SHARE = 1.0
#: The histogram's size bins, as a ratio between neighbours.
BIN_RATIO = 1.02
#: The most cubes the histogram looks at before it says the zones alone are too many elements.
MAX_CUBES = 3_000_000
#: An edge zone (a sharp corner) is sampled along its edge at this share of its size.
EDGE_SAMPLE_SHARE = 0.25
#: THE HISTOGRAM ON EVERY CORE (Areen, 24 Oct: planning the 114-part caldera took 25 minutes on one
#: core - 10 histograms of ~150 s, 60% of it the surface distance, which holds Python's lock). An
#: octree level with at least this many cubes is measured in worker processes, a slice each; every
#: cube's numbers are its own, so the result is the same to the bit as on one core.
PARALLEL_MIN_POINTS = 20_000
#: the most worker processes (FEA_PLAN_WORKERS overrides; 1 = one core, as before)
MAX_WORKERS = 8


@dataclass
class Histogram:
    """(size_m, volume_m3) rows, smallest size first; `inf` for volume no zone reaches. `complete`
    is False when the octree stopped at MAX_CUBES (the zones alone are past any budget); the cubes it
    had not resolved are filed at their zones' size, so the estimate is a floor, never zero."""
    rows: list[tuple[float, float]]
    complete: bool = True
    cubes: int = 0

    def as_list(self) -> list[list[float]]:
        return [[_sig(h), _sig(v)] for h, v in self.rows]

    @staticmethod
    def from_list(rows) -> Histogram:
        return Histogram(rows=[(float(h), float(v)) for h, v in rows or []])

    @property
    def volume_m3(self) -> float:
        return float(sum(v for _h, v in self.rows))


def _sig(v: float) -> float:
    v = float(v)
    return float(f"{v:.6g}") if math.isfinite(v) else v


def _polydata(xyz: np.ndarray, tris: np.ndarray):
    from vtkmodules.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData

    pts = vtkPoints()
    pts.SetData(numpy_to_vtk(np.ascontiguousarray(xyz, dtype=float), deep=True))
    cells = vtkCellArray()
    offsets = np.arange(0, 3 * len(tris) + 1, 3, dtype=np.int64)
    cells.SetData(numpy_to_vtkIdTypeArray(offsets, deep=True),
                  numpy_to_vtkIdTypeArray(np.ascontiguousarray(np.asarray(tris).ravel(), dtype=np.int64),
                                          deep=True))
    pd = vtkPolyData()
    pd.SetPoints(pts)
    pd.SetPolys(cells)
    return pd


class _Distance:
    """Signed distance to a triangle set (negative inside an outward-facing closed surface),
    vectorised over many points."""

    def __init__(self, xyz: np.ndarray, tris: np.ndarray):
        from vtkmodules.vtkFiltersCore import vtkImplicitPolyDataDistance

        self.pd = _polydata(xyz, tris)
        self.f = vtkImplicitPolyDataDistance()
        self.f.SetInput(self.pd)

    def __call__(self, p: np.ndarray) -> np.ndarray:
        from vtkmodules.util.numpy_support import numpy_to_vtk, vtk_to_numpy

        if not len(p):
            return np.zeros(0)
        a = numpy_to_vtk(np.ascontiguousarray(p, dtype=float), deep=True)
        out = numpy_to_vtk(np.zeros(len(p)), deep=True)
        self.f.FunctionValue(a, out)
        return np.asarray(vtk_to_numpy(out), dtype=float).copy()


class _BallDistance:
    """Distance past the nearest of a set of balls (0 inside one)."""

    def __init__(self, balls):
        b = np.asarray(balls, dtype=float).reshape(-1, 4)
        self.c, self.r = b[:, :3], b[:, 3]

    def __call__(self, p: np.ndarray) -> np.ndarray:
        if not len(p):
            return np.zeros(0)
        d = np.full(len(p), np.inf)
        for c, r in zip(self.c, self.r, strict=True):
            d = np.minimum(d, np.maximum(0.0, np.linalg.norm(p - c, axis=1) - r))
        return d


class _EdgeDistance:
    """Distance to a set of polyline segments, by points sampled along them."""

    def __init__(self, a: np.ndarray, b: np.ndarray, spacing: float):
        from scipy.spatial import cKDTree

        pts = [a, b]
        for p, q in zip(a, b, strict=True):
            n = int(min(2000, math.ceil(float(np.linalg.norm(q - p)) / max(spacing, 1e-12))))
            if n > 1:
                t = (np.arange(1, n) / n)[:, None]
                pts.append(p + t * (q - p))
        self.pts = np.unique(np.vstack(pts), axis=0)
        self.tree = cKDTree(self.pts)

    def __call__(self, p: np.ndarray) -> np.ndarray:
        if not len(p):
            return np.zeros(0)
        d, _ = self.tree.query(p)
        return np.asarray(d, dtype=float)


@dataclass
class _Group:
    size: float
    reach: float
    faces: _Distance | None
    edges: _EdgeDistance | None
    balls: _BallDistance | None = None
    #: an assembly part's base (PLAN-fea-17): the size holds everywhere inside the part's own surface
    inside: bool = False
    #: ...and its box: the part's surface is measured only for points in the box (outside it, the
    #: distance to the box stands in - never more, so the size there is never coarser than the rule)
    lo: np.ndarray | None = None
    hi: np.ndarray | None = None


def group_zones(zones: list[dict]) -> list[dict]:
    """The zones in groups of like size (GROUP_RATIO apart), each with its smallest size and largest
    reach - never coarser, never shorter than any zone in it asked. Face zones and edge zones (the
    capped corners, held along the edge two faces share) are grouped apart. Every engine adapter
    builds its fields from these same groups, so the field it meshes is the one estimated here.

        [{"size": m, "reach": m, "edge": bool, "faces": [names], "edges": [[face_a, face_b], ...]}]
    """
    buckets: dict[tuple[int, bool], list[dict]] = {}
    spots: dict[tuple[int, str], list[dict]] = {}
    parts: list[dict] = []
    for z in zones:
        size = float(z["size_m"])
        if size <= 0 or not math.isfinite(size):
            continue
        if z.get("kind") == "part":
            parts.append(z)
            continue
        b = int(math.floor(math.log(size) / math.log(GROUP_RATIO)))
        if z.get("kind") == "wall_spot":
            spots.setdefault((b, str((z.get("faces") or [""])[0])), []).append(z)
            continue
        key = (b, z.get("kind") == "corner")
        buckets.setdefault(key, []).append(z)
    out = []
    for (_b, edge), zs in sorted(buckets.items()):
        faces = sorted({str(f) for z in zs for f in z.get("faces") or []}, key=_face_key)
        out.append({"size": min(float(z["size_m"]) for z in zs),
                    "reach": max(float(z.get("reach_m") or 0.0) for z in zs), "edge": edge,
                    "faces": faces,
                    "edges": [list(z.get("faces") or [])[:2] for z in zs] if edge else []})
    # PLAN-fea-3's thin spots: the wall rule held off the face (out to the wall's own reach) but only
    # inside balls round where the wall is thin - the size is the larger of the two laws, so it grows
    # away from the face and away from the balls alike. After the face and edge groups, so a plan
    # without them groups exactly as before.
    for (_b, face), zs in sorted(spots.items(), key=lambda kv: (kv[0][0], _face_key(kv[0][1]))):
        out.append({"size": min(float(z["size_m"]) for z in zs),
                    "reach": max(float(z.get("reach_m") or 0.0) for z in zs), "edge": False, "faces": [face],
                    "edges": [], "balls": [[*[float(v) for v in z["at_m"]], float(z["radius_m"])] for z in zs]})
    # PLAN-fea-17: AN ASSEMBLY PART'S BASE holds THROUGH THE PART (a ceiling inside its volume, not only
    # on its faces), growing only outside it; one group per part, never merged with another part's
    for z in parts:
        out.append({"size": float(z["size_m"]), "reach": float(z.get("reach_m") or 0.0), "edge": False,
                    "faces": sorted({str(f) for f in z.get("faces") or []}, key=_face_key), "edges": [],
                    "part": str(z.get("part") or z.get("id") or "")})
    return out


def _face_key(name: str) -> tuple[str, int]:
    head, _, tail = str(name).rpartition("_")
    return (head, int(tail)) if tail.isdigit() else (str(name), 0)


class SizeField:
    """The plan's zones on the part: h_zone(x) for many points at once (inf where no zone reaches)."""

    def __init__(self, surface, zones: list[dict], *, growth: float, scale_to_m: float = 1e-3):
        """surface: cad/fea_features.Surface (its own unit; `scale_to_m` turns it into metres).
        zones: the plan's zones (sizes in metres; faces named as the surface names them)."""
        # what a worker process needs to build the same field (plain arrays and lists: they pickle)
        self._spec = ({"xyz": np.asarray(surface.xyz), "tris": np.asarray(surface.tris),
                       "label": np.asarray(surface.label), "names": list(surface.names)},
                      list(zones), float(growth), float(scale_to_m))
        self.growth = float(growth)
        self.xyz = np.asarray(surface.xyz, dtype=float) * float(scale_to_m)
        self.tris = np.asarray(surface.tris)
        self.label = np.asarray(surface.label)
        self.index = {n: k for k, n in enumerate(surface.names)}
        self.solid = _Distance(self.xyz, self.tris)
        self.groups = self._groups(zones)

    def _tris_of(self, faces) -> np.ndarray:
        ks = [self.index[f] for f in faces if f in self.index]
        return np.flatnonzero(np.isin(self.label, ks)) if ks else np.zeros(0, dtype=np.int64)

    def _edges_of(self, faces) -> tuple[np.ndarray, np.ndarray]:
        """The segments two faces share: edges of the welded surface with a triangle of each."""
        ks = [self.index[f] for f in faces[:2] if f in self.index]
        if len(ks) < 2:
            return np.zeros((0, 3)), np.zeros((0, 3))
        sides = []
        for k in ks:
            t = self.tris[self.label == k]
            e = np.sort(np.vstack([t[:, [0, 1]], t[:, [1, 2]], t[:, [2, 0]]]), axis=1)
            sides.append({(int(a), int(b)) for a, b in e})
        shared = sorted(sides[0] & sides[1])
        if not shared:
            return np.zeros((0, 3)), np.zeros((0, 3))
        e = np.asarray(shared, dtype=np.int64)
        return self.xyz[e[:, 0]], self.xyz[e[:, 1]]

    def _compact(self, idx: np.ndarray) -> tuple[_Distance, np.ndarray, np.ndarray]:
        """The distance to the triangles `idx` (their own vertices only) and their box."""
        used, tris = np.unique(self.tris[idx], return_inverse=True)
        pts = self.xyz[used]
        return _Distance(pts, tris.reshape(-1, 3)), pts.min(axis=0), pts.max(axis=0)

    def _groups(self, zones: list[dict]) -> list[_Group]:
        out = []
        for g in group_zones(zones):
            if g.get("part"):
                idx = self._tris_of(g["faces"])
                if len(idx):
                    # the part's own vertices only: one group per part must not copy the whole assembly
                    dist, lo, hi = self._compact(idx)
                    out.append(_Group(g["size"], g["reach"], dist, None, inside=True, lo=lo, hi=hi))
                continue
            if g.get("balls"):
                idx = self._tris_of(g["faces"])
                if not len(idx):
                    continue
                dist, lo, hi = self._compact(idx)
                out.append(_Group(g["size"], g["reach"], dist, None, _BallDistance(g["balls"]), lo=lo, hi=hi))
                continue
            if g["edge"]:
                ends = [self._edges_of(fs) for fs in g["edges"]]
                aa = np.vstack([a for a, _b in ends])
                bb = np.vstack([b for _a, b in ends])
                if not len(aa):
                    continue
                both = np.vstack([aa, bb])
                out.append(_Group(g["size"], g["reach"], None, _EdgeDistance(aa, bb, EDGE_SAMPLE_SHARE * g["size"]),
                                  lo=both.min(axis=0), hi=both.max(axis=0)))
            else:
                # ONE GROUP PER PART of an assembly (a face group spans every part with a zone of that
                # size): each has its own box, so a point far from a part never measures its faces
                by_part: dict[str, list[str]] = {}
                for f in g["faces"]:
                    by_part.setdefault(_face_key(f)[0], []).append(f)
                for faces in by_part.values():
                    idx = self._tris_of(faces)
                    if not len(idx):
                        continue
                    dist, lo, hi = self._compact(idx)
                    out.append(_Group(g["size"], g["reach"], dist, None, lo=lo, hi=hi))
        # finest first, so the size found so far rules out the most measuring (sizes() prunes by it)
        return sorted(out, key=lambda grp: grp.size)

    def zone_size(self, p: np.ndarray) -> np.ndarray:
        """The zones' size at each point (inf where no zone is set)."""
        return self.sizes(p)[0]

    def sizes(self, p: np.ndarray, r: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
        """The zones' size at each point, and the size a cube of half-diagonal `r` round it must be
        resolved to. They differ only for an assembly part's base (PLAN-fea-17): that size is the
        SAME all through the part, so a cube lying `r` deep inside the part needs no finer look for
        it - only a cube across the part's boundary does (and every other zone, as always)."""
        p = np.asarray(p, dtype=float).reshape(-1, 3)
        h = np.full(len(p), np.inf)
        need = np.full(len(p), np.inf)
        g1 = self.growth - 1.0
        for g in self.groups:
            deep = None
            if g.lo is not None and g.hi is not None and not g.inside:
                # PRUNED: the distance to the group's box is a floor on the distance to it, so where even
                # that floor gives no finer size than found so far, the group is not measured (exact)
                floor = np.linalg.norm(np.maximum(0.0, np.maximum(g.lo - p, p - g.hi)), axis=1)
                look = g.size + g1 * np.maximum(0.0, floor - g.reach) < need
                if not look.any():
                    continue
                q = p[look]
                d = np.abs(g.faces(q)) if g.faces is not None else g.edges(q)  # type: ignore[misc]
                past = d - g.reach
                if g.balls is not None:
                    past = np.maximum(past, g.balls(q))
                hg = np.full(len(p), np.inf)
                hg[look] = g.size + g1 * np.maximum(0.0, past)
                h = np.minimum(h, hg)
                need = np.minimum(need, hg)
                continue
            if g.inside:
                # signed: inside the part (negative) the size is the part's own; outside it grows. Only
                # the points in the part's box are measured against its surface (an assembly of many
                # parts must not measure every point against every part)
                sd = np.linalg.norm(np.maximum(0.0, np.maximum(g.lo - p, p - g.hi)), axis=1)
                box = sd <= 0.0
                if box.any():
                    sd[box] = g.faces(p[box])  # type: ignore[misc]
                d = np.maximum(0.0, sd)
                deep = sd <= -r
            else:
                d = np.abs(g.faces(p)) if g.faces is not None else g.edges(p)  # type: ignore[misc]
            past = d - g.reach
            if g.balls is not None:
                # a thin spot: past its wall's reach from the face, or past its balls, whichever is more
                past = np.maximum(past, g.balls(p))
            hg = g.size + g1 * np.maximum(0.0, past)
            h = np.minimum(h, hg)
            need = np.minimum(need, hg if deep is None else np.where(deep, np.inf, hg))
        return h, need

    def signed(self, p: np.ndarray) -> np.ndarray:
        return self.solid(np.asarray(p, dtype=float).reshape(-1, 3))

    def histogram(self, *, coarsest: float, per_h3: float, stop_elements: float = math.inf,
                  workers: int | None = None) -> Histogram:
        n = plan_workers() if workers is None else max(1, int(workers))
        if n <= 1:
            return self._histogram(coarsest=coarsest, per_h3=per_h3, stop_elements=stop_elements, pool=None)
        import multiprocessing as mp
        from concurrent.futures import ProcessPoolExecutor

        # SPAWNED, never forked: the planner runs in a worker thread of an event loop
        with ProcessPoolExecutor(max_workers=n, mp_context=mp.get_context("spawn"),
                                 initializer=_worker_init, initargs=(self._spec,)) as pool:
            return self._histogram(coarsest=coarsest, per_h3=per_h3, stop_elements=stop_elements,
                                   pool=(pool, n))

    def _on(self, pool, what: str, p: np.ndarray, r: float):
        """self.signed(p) or self.sizes(p, r) - in slices on the workers when the level is big enough."""
        if pool is None or len(p) < PARALLEL_MIN_POINTS:
            return self.signed(p) if what == "signed" else self.sizes(p, r)
        ex, n = pool
        k = 4 * n
        parts = list(ex.map(_worker_call, [what] * k, np.array_split(p, k), [r] * k))
        if what == "signed":
            return np.concatenate(parts)
        return np.concatenate([a for a, _b in parts]), np.concatenate([b for _a, b in parts])

    def _histogram(self, *, coarsest: float, per_h3: float, stop_elements: float, pool) -> Histogram:
        """The volume at each zone size, from an octree over the part (see the module text).
        `coarsest` is the largest base size the plan may take: past it nothing is resolved finer.
        Stops (complete=False) once the zones alone pass `stop_elements` or MAX_CUBES."""
        lo, hi = self.xyz.min(axis=0), self.xyz.max(axis=0)
        span = float((hi - lo).max())
        e = min(span, coarsest)
        n = np.maximum(1, np.ceil((hi - lo) / e).astype(int))
        grid = np.stack(np.meshgrid(*[np.arange(k) for k in n], indexing="ij"), axis=-1).reshape(-1, 3)
        centres = lo + (grid + 0.5) * e
        sizes: list[np.ndarray] = []
        vols: list[np.ndarray] = []
        cubes = 0
        zones_only = 0.0
        complete = True
        e_min = coarsest / 2.0 ** 14
        offsets = np.array([[i, j, k] for i in (-1, 1) for j in (-1, 1) for k in (-1, 1)], dtype=float)
        while len(centres):
            cubes += len(centres)
            r = e * math.sqrt(3.0) / 2.0
            sd = self._on(pool, "signed", centres, r)
            keep = sd < r
            centres, sd = centres[keep], sd[keep]
            if not len(centres):
                break
            hz, need = self._on(pool, "sizes", centres, r)
            want = np.minimum(need, coarsest)
            # a cube wholly inside one part is resolved for every zone but that part's own size, which
            # is the same all through it (PLAN-fea-17)
            leaf = (e <= LEAF_SHARE * want) | (e <= e_min)
            if leaf.any():
                frac = np.clip(0.5 - sd[leaf] / e, 0.0, 1.0)
                v = frac * e ** 3
                sizes.append(hz[leaf])
                vols.append(v)
                fine = np.isfinite(hz[leaf])
                zones_only += float(np.sum(v[fine] / np.minimum(hz[leaf][fine], coarsest) ** 3)) * per_h3
            rest = centres[~leaf]
            if zones_only > stop_elements or cubes + 8 * len(rest) > MAX_CUBES:
                complete = not len(rest)
                if len(rest):
                    # STOPPED SHORT: the cubes not yet resolved are still counted, at the size their
                    # zones ask for - an unfinished histogram is a floor on the estimate, never zero
                    # (fea0221: the octree stopped before any leaf, the estimate read 0 elements and
                    # the time guard fell to its floor)
                    sd_rest = sd[~leaf]
                    sizes.append(hz[~leaf])
                    vols.append(np.clip(0.5 - sd_rest / e, 0.0, 1.0) * e ** 3)
                break
            e /= 2.0
            centres = (rest[:, None, :] + offsets[None, :, :] * (e / 2.0)).reshape(-1, 3)
        h = np.concatenate(sizes) if sizes else np.zeros(0)
        v = np.concatenate(vols) if vols else np.zeros(0)
        return Histogram(rows=_binned(h, v), complete=complete, cubes=int(cubes))


def plan_workers() -> int:
    """How many processes the histogram may use: FEA_PLAN_WORKERS, else the job's CPU quota (never
    the host's cores - Cloud Run's os.cpu_count() is the host's), at most MAX_WORKERS."""
    import os
    raw = os.environ.get("FEA_PLAN_WORKERS")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            return 1
    from meshpipeline.engines.snappy.native import _container_cpus
    return max(1, min(MAX_WORKERS, _container_cpus(0) or os.cpu_count() or 1))


_FIELD: SizeField | None = None


def _worker_init(spec) -> None:
    global _FIELD
    from types import SimpleNamespace
    surf, zones, growth, scale = spec
    _FIELD = SizeField(SimpleNamespace(**surf), zones, growth=growth, scale_to_m=scale)


def _worker_call(what: str, p: np.ndarray, r: float):
    assert _FIELD is not None
    return _FIELD.signed(p) if what == "signed" else _FIELD.sizes(p, r)


def _binned(h: np.ndarray, v: np.ndarray) -> list[tuple[float, float]]:
    """Volume summed into size bins BIN_RATIO apart, each filed under its bin's smallest size."""
    rows: list[tuple[float, float]] = []
    inf = ~np.isfinite(h)
    if inf.any():
        rows.append((math.inf, float(v[inf].sum())))
    h, v = h[~inf], v[~inf]
    keep = v > 0
    h, v = h[keep], v[keep]
    if len(h):
        k = np.floor(np.log(h) / math.log(BIN_RATIO)).astype(np.int64)
        for b in np.unique(k):
            sel = k == b
            rows.append((float(BIN_RATIO ** int(b)), float(v[sel].sum())))
    rows.sort(key=lambda r: r[0])
    return rows


def elements(hist: Histogram | list, base: float, per_h3: float) -> float:
    """The element estimate at base size `base` (metres)."""
    rows = hist.rows if isinstance(hist, Histogram) else Histogram.from_list(hist).rows
    return float(per_h3 * sum(v / min(h, base) ** 3 for h, v in rows))


__all__ = ["Histogram", "SizeField", "elements", "group_zones"]
