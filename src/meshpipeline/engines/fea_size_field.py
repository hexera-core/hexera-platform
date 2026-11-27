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

THIN WALLS AT COARSE SIZES (PLAN-fea-31). Where the element size passes about two-thirds of a wall's
thickness, the volume count above under-counts: the mesher puts a layer of elements on every surface
triangle, so a wall of thickness t at size h holds about SLAB_PER_AREA * area / h^2 elements, not
PER_H3 * area * t / h^3 (a 1 mm plate at 6 mm elements: 523 built, 48 by volume alone). So the surface
is read too - each triangle's size and its face's measured wall - as (size, wall, area) rows, and each
face adds what its triangles hold past its share of the wall's volume:

    extra(base) = sum(area / h^2 * max(0, SLAB_PER_AREA - per_h3 / 2 * t / h)),  h = min(h_zone, base)

which is nothing where the size is under about two-thirds of the wall (every rule-sized wall).
"""
from __future__ import annotations

import copy
import math
import threading
from contextlib import contextmanager
from dataclasses import dataclass

import numpy as np

#: Zones whose sizes are within this ratio share one distance structure (their smallest size and
#: largest reach: never coarser than any of them asked).
GROUP_RATIO = 1.2
#: ...and whose reaches, in their own sizes, are within this ratio (PLAN-fea-31)
REACH_RATIO = 1.5
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
#: the most worker processes (1 = one core, as before)
MAX_WORKERS = 8
#: PLAN-fea-31: elements per face area / h^2 on a wall thinner than the size there - measured on gmsh's own fill
#: (lab ~/feabudget_lab/valid: plates 0.5-2 mm thick at 0.5-16 times their thickness gave 3.6-3.8 per area / h^2,
#: both faces of the plate counted)
SLAB_PER_AREA = 3.7
#: PLAN-fea-31: the elements every surface triangle's layer adds, per area / h^2 at the surface (fitted with
#: engines/fea_plan.PER_H3 on 32 real builds; see there)
SURFACE_PER_AREA = 2.8
#: the surface rows' bins for the wall (a ratio between neighbours)
WALL_BIN_RATIO = 1.1
#: TIME-12: THE POINTS ARE MEASURED IN TILES of about this many (SizeField.sizes): a zone group is looked at only in
#: the tiles it can still make finer, so a part with ~1,800 hot spots (as many groups) no longer costs every point
#: x every group. 0 measures every point against every group's box, as before (the answer is the same to the bit).
TILE_POINTS = 128
#: TIME-12: THE SIGNED DISTANCE TO THE WHOLE SURFACE IS KEPT PER CUBE for one sizing (the geometry does not change
#: between its passes, only the zones' sizes): at most this many cubes (16 bytes each: its key and its distance);
#: past it, a cube not kept is measured as before. 0 keeps none (every cube measured each count, as before).
SIGNED_KEEP_CUBES = 20_000_000
#: TIME-12: a capped corner's edge distance is searched this far (metres) past where its size stops mattering, so a
#: point it reports none for has a size at least this x (growth - 1) over what it needs - far past any rounding.
#: None searches every point to its nearest sample, as before.
EDGE_MARGIN_M: float | None = 1e-9
#: TIME-12: a thin spot's balls are measured before its face, and the face only where the balls leave the size under
#: what the point needs (False: the face first, as before; the same sizes either way)
BALLS_FIRST = True


@dataclass
class Histogram:
    """(size_m, volume_m3) rows, smallest size first; `inf` for volume no zone reaches. `complete`
    is False when the octree stopped at MAX_CUBES (the zones alone are past any budget); the cubes it
    had not resolved are filed at their zones' size, so the estimate is a floor, never zero."""
    rows: list[tuple[float, float]]
    complete: bool = True
    cubes: int = 0
    #: why an unfinished histogram stopped: "cubes" (its cube budget) or "elements" (the zones alone past
    #: the count it was asked to reach); "" when it finished
    stopped: str = ""
    #: PLAN-fea-31: (size_m, wall_m, area_m2) of the surface, binned - the thin walls' own count at coarse
    #: sizes; empty when no face has a measured wall (the count is the volume's alone, as before)
    surface: list[tuple[float, float, float]] | None = None

    def as_list(self) -> list[list[float]]:
        return [[_sig(h), _sig(v)] for h, v in self.rows]

    def surface_list(self) -> list[list[float]]:
        return [[_sig(h), _sig(t), _sig(a)] for h, t, a in self.surface or []]

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


class _SignedKept:
    """TIME-12: the signed distance to the whole surface at the octree's cubes, kept for one sizing.

    A cube is named by its octree level and its integer place at that level, on the grid its count started from (the
    surface's box corner and the first cube's edge: the same in every pass of a sizing that keeps its coarsest size).
    Every cube's centre is made from its parent's by the same arithmetic in every count, so a cube named alike has
    the same centre to the bit, and its distance is the one measuring it again would give: kept distances are the
    same floats. One sorted key array and its distances per (grid, level); at most `cap` cubes in all."""

    def __init__(self, cap: int):
        self.cap = int(cap)
        self.kept: dict[tuple, tuple[np.ndarray, np.ndarray]] = {}
        self.cubes = 0
        self.hits = 0

    @staticmethod
    def keys(centres: np.ndarray, lo: np.ndarray, e: float, n: np.ndarray, level: int) -> np.ndarray | None:
        """Each cube's integer place at its level, packed in one int64 (None: the level's grid does not fit)."""
        m = np.asarray(n, dtype=np.int64) << int(level)
        bits = [max(1, int(v - 1).bit_length()) for v in m.tolist()]
        if sum(bits) > 62:
            return None
        ijk = np.clip(np.floor((centres - lo) / e).astype(np.int64), 0, m - 1)
        return (ijk[:, 0] << (bits[1] + bits[2])) | (ijk[:, 1] << bits[2]) | ijk[:, 2]

    def take(self, at: tuple, keys: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """The kept distances (NaN where not kept) and which cubes are not kept."""
        out = np.full(len(keys), np.nan)
        got = self.kept.get(at)
        if got is None or not len(got[0]):
            return out, np.ones(len(keys), dtype=bool)
        k, v = got
        pos = np.minimum(np.searchsorted(k, keys), len(k) - 1)
        hit = k[pos] == keys
        out[hit] = v[pos[hit]]
        self.hits += int(hit.sum())
        return out, ~hit

    def put(self, at: tuple, keys: np.ndarray, sd: np.ndarray) -> None:
        """Keep these cubes' distances (none of them kept yet), as many as the cap leaves room for."""
        room = self.cap - self.cubes
        if room <= 0 or not len(keys):
            return
        keys, sd = keys[:room], sd[:room]
        self.cubes += len(keys)
        got = self.kept.get(at)
        if got is not None:
            keys, sd = np.concatenate([got[0], keys]), np.concatenate([got[1], sd])
        order = np.argsort(keys, kind="stable")
        self.kept[at] = (keys[order], sd[order])


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

    def __call__(self, p: np.ndarray, bound: float = math.inf) -> np.ndarray:
        """The distance to the nearest sample (inf where none lies within `bound`)."""
        if not len(p):
            return np.zeros(0)
        d, _ = self.tree.query(p, distance_upper_bound=bound) if math.isfinite(bound) else self.tree.query(p)
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
    buckets: dict[tuple[int, bool, int], list[dict]] = {}
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
        # ...and of like REACH, in the zone's own sizes (PLAN-fea-31): a group holds its smallest size out to its
        # largest reach, so a zone whose size drifted into a group of far-reaching zones took their reach - the size
        # field (and the count) jumped as zones crossed size bands (a 9-part assembly: 842k at one factor, 591k at a
        # factor 0.6% coarser)
        reach = float(z.get("reach_m") or 0.0)
        rb = int(math.floor(math.log(reach / size) / math.log(REACH_RATIO))) if reach > 0 else -(10 ** 6)
        key = (b, z.get("kind") == "corner", rb)
        buckets.setdefault(key, []).append(z)
    out = []
    for (_b, edge, _rb), zs in sorted(buckets.items()):
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

    def __init__(self, surface, zones: list[dict], *, growth: float, scale_to_m: float = 1e-3,
                 walls: dict[str, float] | None = None):
        """surface: cad/fea_features.Surface (its own unit; `scale_to_m` turns it into metres).
        zones: the plan's zones (sizes in metres; faces named as the surface names them).
        walls: face name -> its measured wall in metres, for PLAN-fea-31's surface rows."""
        self.walls = {str(k): float(v) for k, v in (walls or {}).items()
                      if v and math.isfinite(float(v)) and float(v) > 0}
        # what a worker process needs to build the same field (plain arrays and lists: they pickle)
        self._spec = ({"xyz": np.asarray(surface.xyz), "tris": np.asarray(surface.tris),
                       "label": np.asarray(surface.label), "names": list(surface.names)},
                      list(zones), float(growth), float(scale_to_m))
        self.growth = float(growth)
        self.xyz = np.asarray(surface.xyz, dtype=float) * float(scale_to_m)
        self.tris = np.asarray(surface.tris)
        self.label = np.asarray(surface.label)
        self.index = {n: k for k, n in enumerate(surface.names)}
        # TIME-12: within one sizing (sizing()) the surface does not change between passes, only the zones: the
        # distance to the whole surface and to each set of triangles a group measures are built once and used again
        # (the same arrays, so the same answers to the bit)
        # (only the last field's are kept: a set no group of this field measures is let go)
        scope = _scope()
        kept = scope.dists
        self._dists: dict[bytes, tuple] = {}
        self._was: dict[bytes, tuple] = {}
        # ...and so are each face's triangles and the segments two faces share (the surface's own), and the sampled
        # edge distance of a capped corner group (its segments and spacing alike: the same tree, the same answers)
        self._edge_sets: dict[tuple, _EdgeDistance] = {}
        self._edge_was: dict[tuple, _EdgeDistance] = {}
        self._shared: dict[tuple[int, int], tuple[np.ndarray, np.ndarray]]
        if kept is not None and kept["scale"] == float(scale_to_m) and np.array_equal(kept["xyz"], self.xyz) \
                and np.array_equal(kept["tris"], self.tris) and np.array_equal(kept["label"], self.label):
            self.solid, self._was = kept["solid"], kept["sets"]
            self._signed_kept: _SignedKept | None = kept.get("signed_kept") if SIGNED_KEEP_CUBES > 0 else None
            self._face_tris, self._shared = kept["face_tris"], kept["shared"]
            self._edge_was = kept["edge_sets"]
        else:
            self.solid = _Distance(self.xyz, self.tris)
            self._signed_kept = None
            order = np.argsort(self.label, kind="stable")
            cut = np.searchsorted(self.label[order], np.arange(len(self.index) + 1))
            self._face_tris = (order, cut)
            self._shared = {}
        # ...and so is the signed distance to it at every cube a count measured (_SignedKept: the same floats)
        if self._signed_kept is None and SIGNED_KEEP_CUBES > 0:
            self._signed_kept = _SignedKept(SIGNED_KEEP_CUBES)
        if scope.depth:
            scope.dists = {"scale": float(scale_to_m), "xyz": self.xyz, "tris": self.tris, "solid": self.solid,
                           "sets": self._dists, "signed_kept": self._signed_kept, "label": self.label,
                           "face_tris": self._face_tris, "shared": self._shared, "edge_sets": self._edge_sets}
        self.groups = self._groups(zones)
        self._was, self._edge_was = {}, {}
        # TIME-12: what this field's counts found, answered again to the bit (histogram): each count made, the
        # finished ones, where those stopped on their cube budget stood (a recount with more goes on from there),
        # and the surface rows at each coarsest size
        self._done: dict = {}
        self._whole: list = []
        self._held: list[dict] = []
        self._rows: dict[float, list] = {}

    def _tris_of(self, faces) -> np.ndarray:
        """The faces' triangles, in the surface's order (as np.flatnonzero(np.isin(label, faces)), from each face's
        own triangles found once per surface - it was a pass over every triangle for every group, every pass)."""
        ks = sorted({self.index[f] for f in faces if f in self.index})
        if not ks:
            return np.zeros(0, dtype=np.int64)
        order, cut = self._face_tris
        return np.sort(np.concatenate([order[cut[k]:cut[k + 1]] for k in ks])).astype(np.int64, copy=False)

    def _edges_of(self, faces) -> tuple[np.ndarray, np.ndarray]:
        """The segments two faces share: edges of the welded surface with a triangle of each (found once per pair
        of faces in a sizing: the surface's own)."""
        ks = [self.index[f] for f in faces[:2] if f in self.index]
        if len(ks) < 2:
            return np.zeros((0, 3)), np.zeros((0, 3))
        got = self._shared.get((ks[0], ks[1]))
        if got is None:
            got = self._shared[(ks[0], ks[1])] = self._edges_found(ks)
        return got

    def _edges_found(self, ks: list[int]) -> tuple[np.ndarray, np.ndarray]:
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
        key = np.asarray(idx, dtype=np.int64).tobytes()
        got = self._dists.get(key) or self._was.get(key)
        if got is None:
            used, tris = np.unique(self.tris[idx], return_inverse=True)
            pts = self.xyz[used]
            got = (_Distance(pts, tris.reshape(-1, 3)), pts.min(axis=0), pts.max(axis=0))
        self._dists[key] = got
        return got

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
                spacing = EDGE_SAMPLE_SHARE * g["size"]
                key = (aa.tobytes(), bb.tobytes(), float(spacing))
                edge = self._edge_sets.get(key) or self._edge_was.get(key) or _EdgeDistance(aa, bb, spacing)
                self._edge_sets[key] = edge
                out.append(_Group(g["size"], g["reach"], None, edge, lo=both.min(axis=0), hi=both.max(axis=0)))
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
        if TILE_POINTS > 0 and len(p) >= 2 * TILE_POINTS and len(self.groups) > 1 and self.growth >= 1.0:
            return self._sizes_tiled(p, r)
        return self._sizes_each(p, r)

    def _sizes_tiled(self, p: np.ndarray, r: float) -> tuple[np.ndarray, np.ndarray]:
        """sizes(), THE SAME TO THE BIT, a tile of points at a time (TIME-12). The points are sorted into tiles (a grid
        over them, about TILE_POINTS each) and each tile keeps the largest size its points still need. A group is
        measured only in the tiles where the floor its box gives the tile's box (never more than the floor at any of
        its points: every step of it is monotone) is under that largest need; elsewhere no point of the tile would
        pass sizes()'s own test, and an assembly part's base there is never finer than what the tile has (its size
        is never under the floor). In the tiles kept, every point is tested and measured exactly as _sizes_each
        does: each point's numbers are its own, so the sizes are the same to the bit."""
        n = len(p)
        lo, hi = p.min(axis=0), p.max(axis=0)
        # a grid of near-cubic tiles over the points' box, about n / TILE_POINTS of them
        span = np.maximum(hi - lo, 1e-3 * max(float((hi - lo).max()), 1e-30))     # a flat set: one tile thick
        cell = (float(np.prod(span)) * TILE_POINTS / n) ** (1.0 / 3.0)
        m = np.maximum(1, np.minimum(np.ceil(span / cell), 1 << 20)).astype(np.int64)
        ijk = np.minimum(m - 1, ((p - lo) / span * m).astype(np.int64))
        key = (ijk[:, 0] * m[1] + ijk[:, 1]) * m[2] + ijk[:, 2]
        order = np.argsort(key, kind="stable")
        key = key[order]
        ps = p[order]
        starts = np.flatnonzero(np.concatenate([[True], key[1:] != key[:-1]]))
        counts = np.diff(np.concatenate([starts, [n]]))
        tlo = np.minimum.reduceat(ps, starts, axis=0)
        thi = np.maximum.reduceat(ps, starts, axis=0)
        h = np.full(n, np.inf)
        need = np.full(n, np.inf)
        most = np.full(len(starts), np.inf)              # each tile's largest need: never under any of its points'
        everyone = np.arange(n)
        g1 = self.growth - 1.0
        for g in self.groups:
            if g.lo is not None and g.hi is not None:
                floor = np.linalg.norm(np.maximum(0.0, np.maximum(g.lo - thi, tlo - g.hi)), axis=1)
                sel = g.size + g1 * np.maximum(0.0, floor - g.reach) < most
                if not sel.any():
                    continue
                c = counts[sel]
                ends = np.cumsum(c)
                idx = np.repeat(starts[sel] - (ends - c), c) + np.arange(int(ends[-1]))
                first = ends - c
            else:
                sel, idx, first = slice(None), everyone, starts
            hs, ns = self._sizes_each(ps[idx], r, h[idx], need[idx], (g,))
            h[idx] = hs
            need[idx] = ns
            most[sel] = np.maximum.reduceat(ns, first)
        out_h = np.empty(n)
        out_need = np.empty(n)
        out_h[order] = h
        out_need[order] = need
        return out_h, out_need

    def _sizes_each(self, p: np.ndarray, r: float, h: np.ndarray | None = None, need: np.ndarray | None = None,
                    groups=None) -> tuple[np.ndarray, np.ndarray]:
        """sizes() for every point against every group's box (or `groups`, going on from `h` and `need`)."""
        if h is None or need is None:
            h = np.full(len(p), np.inf)
            need = np.full(len(p), np.inf)
        g1 = self.growth - 1.0
        for g in self.groups if groups is None else groups:
            deep = None
            if g.lo is not None and g.hi is not None and not g.inside:
                # PRUNED: the distance to the group's box is a floor on the distance to it, so where even
                # that floor gives no finer size than found so far, the group is not measured (exact)
                floor = np.linalg.norm(np.maximum(0.0, np.maximum(g.lo - p, p - g.hi)), axis=1)
                look = g.size + g1 * np.maximum(0.0, floor - g.reach) < need
                if not look.any():
                    continue
                ball = None
                if g.balls is not None and BALLS_FIRST:
                    # TIME-12: A THIN SPOT'S BALLS FIRST: its size is never under size + (growth - 1) x the distance
                    # past its balls (it takes the larger of that and the face's law), so where that alone is not
                    # under the need, the face is not measured - its size there could change nothing (exact: the
                    # same monotone steps as the size itself)
                    ball = g.balls(p[look])
                    near = g.size + g1 * np.maximum(0.0, ball) < need[look]
                    look[look] = near
                    ball = ball[near]
                    if not look.any():
                        continue
                q = p[look]
                if g.faces is not None:
                    d = np.abs(g.faces(q))
                else:
                    d = self._edge_reach(g, q, need[look])
                past = d - g.reach
                if g.balls is not None:
                    past = np.maximum(past, g.balls(q) if ball is None else ball)
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
                if g1 >= 0.0:
                    # TIME-12: nor where a finer zone already holds the point to the part's own size or under it - the
                    # part's size is never under its own anywhere (inside it is that size, outside it grows), so it
                    # could change neither the size nor what the cube needs there: left at 0, it gives that size
                    box &= need > g.size
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

    def _edge_reach(self, g: _Group, q: np.ndarray, need: np.ndarray) -> np.ndarray:
        """A capped corner group's distance at `q`, its tree searched only as far as it could still matter (TIME-12):
        past reach + (need - size) / (growth - 1) the group's size is at least what the point needs, so it changes
        neither the size nor the need there. The search goes EDGE_MARGIN_M past the farthest such point and returns
        inf beyond (where the size would only be larger than the need: the same min, to the bit); within it, the
        nearest sample is the one found without the limit, at the same distance."""
        assert g.edges is not None
        g1, margin = self.growth - 1.0, EDGE_MARGIN_M
        if g1 <= 0.0 or margin is None:
            return g.edges(q)
        lim = g.reach + (need - g.size) / g1
        free = ~np.isfinite(lim)
        if free.all():
            return g.edges(q)
        d = np.empty(len(q))
        if free.any():
            d[free] = g.edges(q[free])
        bound = max(0.0, float(lim[~free].max())) + margin
        d[~free] = g.edges(q[~free], bound)
        return d

    def signed(self, p: np.ndarray) -> np.ndarray:
        return self.solid(np.asarray(p, dtype=float).reshape(-1, 3))

    def histogram(self, *, coarsest: float, per_h3: float, stop_elements: float = math.inf,
                  workers: int | None = None, max_cubes: int | None = None) -> Histogram:
        n = plan_workers() if workers is None else max(1, int(workers))
        cap = MAX_CUBES if max_cubes is None else int(max_cubes)
        at, stop = (float(coarsest), float(per_h3)), float(stop_elements)
        # TIME-12: A COUNT THIS FIELD HAS MADE ANSWERS AGAIN, to the bit. Asked once more alike; or, finished, for any
        # stop it never came near and as many cubes or more (no level of it stopped, and none would)
        done = self._done.get((at, stop, cap))
        if done is None:
            done = next((h for a, z, m, h in self._whole if a == at and stop >= z and cap >= m), None)
        if done is not None:
            return copy.deepcopy(done)
        # ...and one that stopped on its cube budget is gone on from, for a stop it never came near
        held = None
        for st in self._held:
            if st["at"] == at and stop >= st["zones_only"] and cap >= st["max_cubes"]:
                if held is None or st["max_cubes"] > held["max_cubes"]:
                    held = st
        # PLAIN CHILD PROCESSES, never forked (the planner runs in a worker thread of an event loop) and
        # never multiprocessing's spawn (it re-imports the caller's main module, which a script without
        # a main guard runs again): `python -m` this module, fed by pipes - started only when a level is
        # measured
        pool = _LazyPool(self._spec, n) if n > 1 else None
        failed = True
        try:
            hist = self._histogram(coarsest=coarsest, per_h3=per_h3, stop_elements=stop_elements, pool=pool,
                                   max_cubes=cap, held=held)
            failed = False
        finally:
            if pool is not None:
                pool.close(failed=failed)
        self._done[(at, stop, cap)] = copy.deepcopy(hist)
        return hist

    def _surface_rows(self, pool, coarsest: float) -> list[tuple[float, float, float]]:
        """PLAN-fea-31: every surface triangle of a face with a measured wall, at the zone size on it,
        binned by size and wall: [(size_m, wall_m, area_m2)]."""
        if not len(self.tris):
            return []
        if float(coarsest) in self._rows:
            return list(self._rows[float(coarsest)])
        wall_of = np.zeros(len(self.index) + 1)
        for name, k in self.index.items():
            wall_of[k] = self.walls.get(name, 0.0)
        lab = np.asarray(self.label, dtype=np.int64)
        known = (lab >= 0) & (lab < len(self.index))
        t = np.zeros(len(self.tris))
        t[known] = wall_of[lab[known]]
        tri = self.xyz[self.tris]
        area = 0.5 * np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1)
        h = np.minimum(np.asarray(self._on(pool, "sizes", tri.mean(axis=1), 0.0)[0], dtype=float), coarsest)
        kh = np.floor(np.log(h) / math.log(BIN_RATIO)).astype(np.int64)
        # a face with no measured wall is filed under wall 0 (its slab term is not read)
        kt = np.where(t > 0, np.floor(np.log(np.where(t > 0, t, 1.0)) / math.log(WALL_BIN_RATIO)), -10**6)
        rows: dict[tuple[int, int], float] = {}
        for a, b, v in zip(kh.tolist(), kt.astype(np.int64).tolist(), area.tolist(), strict=True):
            rows[(a, b)] = rows.get((a, b), 0.0) + v
        # each bin filed under its smallest size and its largest wall: never more than it holds
        out = sorted((float(BIN_RATIO ** a), float(WALL_BIN_RATIO ** (b + 1)) if b > -10**6 else 0.0, v)
                     for (a, b), v in rows.items() if v > 0)
        self._rows[float(coarsest)] = list(out)
        return out

    def _on(self, pool, what: str, p: np.ndarray, r: float):
        """self.signed(p) or self.sizes(p, r) - in slices on the workers when the level is big enough."""
        if pool is None or len(p) < PARALLEL_MIN_POINTS:
            return self.signed(p) if what == "signed" else self.sizes(p, r)
        parts = pool.get().map(what, np.array_split(p, 4 * pool.n), r)
        if what == "signed":
            return np.concatenate(parts)
        return np.concatenate([a for a, _b in parts]), np.concatenate([b for _a, b in parts])

    def _signed_at(self, pool, centres: np.ndarray, r: float, at: tuple, lo: np.ndarray, e: float,
                   n: np.ndarray) -> np.ndarray:
        """The signed distance at an octree level's cube centres: kept ones read back (TIME-12, _SignedKept: the same
        floats), the rest measured and kept while the cap has room."""
        kept = self._signed_kept
        keys = None if kept is None else kept.keys(centres, lo, e, n, at[1])
        if kept is None or keys is None:
            return self._on(pool, "signed", centres, r)
        sd, miss = kept.take(at, keys)
        if miss.any():
            got = np.asarray(self._on(pool, "signed", centres[miss], r), dtype=float)
            sd[miss] = got
            kept.put(at, keys[miss], got)
        return sd

    def _histogram(self, *, coarsest: float, per_h3: float, stop_elements: float, pool,
                   max_cubes: int = MAX_CUBES, held: dict | None = None) -> Histogram:
        """The volume at each zone size, from an octree over the part (see the module text).
        `coarsest` is the largest base size the plan may take: past it nothing is resolved finer.
        Stops (complete=False) once the zones alone pass `stop_elements` or MAX_CUBES. `held`: where an
        earlier count of this field stopped on its cube budget, gone on from (histogram)."""
        e_min = coarsest / 2.0 ** 14
        offsets = np.array([[i, j, k] for i in (-1, 1) for j in (-1, 1) for k in (-1, 1)], dtype=float)
        sizes: list[np.ndarray] = []
        vols: list[np.ndarray] = []
        complete = True
        stopped = ""
        at = (float(coarsest), float(per_h3))
        if held is not None:
            # TIME-12: A RECOUNT WITH MORE CUBES GOES ON FROM WHERE A COUNT OF THIS FIELD STOPPED on its cube
            # budget: every level before it is the same to the bit, so counting it again from the first cube only
            # repeats it (the full plan of a 6-part coil counted 3 M cubes, then 12 M from nothing)
            e, cubes, zones_only = held["e"], held["cubes"], held["zones_only"]
            sizes, vols = list(held["sizes"]), list(held["vols"])
            rest, sd_rest, hz_rest = held["rest"], held["sd_rest"], held["hz_rest"]
            lo, n, level = held["lo"], held["n"], held["level"]
            centres = np.zeros((0, 3))
        else:
            lo, hi = self.xyz.min(axis=0), self.xyz.max(axis=0)
            span = float((hi - lo).max())
            e = min(span, coarsest)
            n = np.maximum(1, np.ceil((hi - lo) / e).astype(int))
            grid = np.stack(np.meshgrid(*[np.arange(k) for k in n], indexing="ij"), axis=-1).reshape(-1, 3)
            centres = lo + (grid + 0.5) * e
            cubes = 0
            zones_only = 0.0
            rest = None
            level = 0
        # the grid this count's cubes are named on (_SignedKept): its corner, its first cube's edge and its shape
        start = (np.asarray(lo, dtype=float).tobytes(), float(e) * 2.0 ** level, tuple(int(k) for k in n))
        while rest is not None or len(centres):
            if rest is None:
                cubes += len(centres)
                r = e * math.sqrt(3.0) / 2.0
                sd = self._signed_at(pool, centres, r, (start, level), lo, e, n)
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
                rest, sd_rest, hz_rest = centres[~leaf], sd[~leaf], hz[~leaf]
            if zones_only > stop_elements or cubes + 8 * len(rest) > max_cubes:
                complete = not len(rest)
                if not complete:
                    stopped = "elements" if zones_only > stop_elements else "cubes"
                if stopped == "cubes" and max_cubes < HOLD_MAX_CUBES and \
                        not any(st["at"] == at and st["max_cubes"] == int(max_cubes) for st in self._held):
                    self._held.append({"at": at, "max_cubes": int(max_cubes), "e": e, "cubes": cubes,
                                       "zones_only": zones_only, "sizes": list(sizes), "vols": list(vols),
                                       "rest": rest, "sd_rest": sd_rest, "hz_rest": hz_rest, "lo": lo, "n": n,
                                       "level": level})
                if len(rest):
                    # STOPPED SHORT: the cubes not yet resolved are still counted, at the size their
                    # zones ask for - an unfinished histogram is a floor on the estimate, never zero
                    # (fea0221: the octree stopped before any leaf, the estimate read 0 elements and
                    # the time guard fell to its floor)
                    sizes.append(hz_rest)
                    vols.append(np.clip(0.5 - sd_rest / e, 0.0, 1.0) * e ** 3)
                break
            e /= 2.0
            level += 1
            centres = (rest[:, None, :] + offsets[None, :, :] * (e / 2.0)).reshape(-1, 3)
            rest = None
        h = np.concatenate(sizes) if sizes else np.zeros(0)
        v = np.concatenate(vols) if vols else np.zeros(0)
        hist = Histogram(rows=_binned(h, v), complete=complete, cubes=int(cubes), stopped=stopped,
                         surface=self._surface_rows(pool, coarsest))
        if complete:
            self._whole.append((at, zones_only, int(max_cubes), copy.deepcopy(hist)))
        return hist


#: the rule a field's kept counts follow (a count is never made twice, the answer the same to the bit)
COUNTED_ONCE_RULE = "TIME-12"
#: TIME-12: a count stopped on a cube budget of this many cubes or more is not held for a recount (nothing recounts
#: past engines/fea_driver.MAX_CUBES_CEILING, and what it holds is the size of the count)
HOLD_MAX_CUBES = 12_000_000


class _Scope(threading.local):
    """One thread's sizing (sizing()): how deep, its counting processes, the last field's surface distances."""

    depth = 0
    pool: _Pool | None = None
    dists: dict | None = None


_SCOPES = _Scope()


def _scope() -> _Scope:
    return _SCOPES


@contextmanager
def sizing():
    """TIME-12: ONE SIZING'S PASSES SHARE what does not change between them: the counting processes (started once,
    each told the next pass's zones - it was 8 new processes for every count, each loading the field from nothing)
    and the surface distances (the whole surface, and every set of triangles a group measures, kept from the last
    field). Nested, the outermost one ends them. The answers are the same to the bit."""
    scope = _scope()
    scope.depth += 1
    try:
        yield
    finally:
        scope.depth -= 1
        if scope.depth == 0:
            pool, scope.pool, scope.dists = scope.pool, None, None
            if pool is not None:
                pool.close()


class _LazyPool:
    """A _Pool started when a level is first measured on it (a count answered from what it holds starts none);
    within sizing(), the sizing's own processes, told this field."""

    def __init__(self, spec, n: int):
        self.spec, self.n = spec, int(n)
        self.pool: _Pool | None = None
        self.shared = False

    def get(self) -> _Pool:
        if self.pool is None:
            scope = _scope()
            if scope.depth and scope.pool is not None and scope.pool.n == self.n:
                scope.pool.init(self.spec)
                self.pool, self.shared = scope.pool, True
            else:
                self.pool = _Pool(self.spec, self.n)
                if scope.depth:
                    if scope.pool is not None:
                        scope.pool.close()
                    scope.pool, self.shared = self.pool, True
        return self.pool

    def close(self, failed: bool = False) -> None:
        if self.pool is None:
            return
        if self.shared and failed:
            scope = _scope()
            if scope.pool is self.pool:
                scope.pool = None        # a pool a count failed on is not told another field
            self.pool.close()
        elif not self.shared:
            self.pool.close()


#: TIME-21: ON A SIZING VM OF ITS OWN the counting processes follow the machine (machine_workers) instead of
#: MAX_WORKERS - its CPU quota, bounded by its memory and by this hard ceiling (the speed machine's 32 vCPUs). Set for
#: one sizing by the sizing's own process (engines/fea_plan_options `--workers-cap`); None everywhere else.
VM_MAX_WORKERS = 32
#: what one counting process is given of the machine's memory: it holds the whole surface (its vtk locator) and each
#: pass's zone groups. Measured: about 0.5 GB on the lab's parts, 2 GB on the largest (grooved_shaft at 1.7 M
#: triangles, sizing round 4); this is that largest one and a quarter more
CHILD_GIB = 2.5
#: what the sizing's own process is given: 4 GB measured after the measure on drilled_plates(30) (round 4), the kept
#: signed distance at its cap (SIGNED_KEEP_CUBES x 16 bytes = 0.32 GB) and a little more
PARENT_GIB = 4.5
#: of the machine's memory, what a sizing may use (contracts/mesh_machine.USABLE_SHARE: the OS and the container
#: runtime keep the rest)
MEMORY_SHARE = 0.9
_WORKERS_CAP: int | None = None


def set_workers_cap(cap: int | None) -> None:
    """TIME-21: this process's counting processes may be as many as `cap` (machine_workers) instead of MAX_WORKERS;
    None puts MAX_WORKERS back. The answer is the same to the bit whatever the count (TIME-12)."""
    global _WORKERS_CAP
    _WORKERS_CAP = max(1, int(cap)) if cap else None


def memory_gib() -> float:
    """The memory this process may use, in GiB: the container's cgroup limit when it has one, else the machine's."""
    from pathlib import Path as _P
    try:
        raw = _P("/sys/fs/cgroup/memory.max").read_text().strip()
        if raw and raw != "max":
            return int(raw) / 1024.0 ** 3
    except (OSError, ValueError):
        pass
    try:
        for line in _P("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) / 1024.0 ** 2
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def machine_workers(*, cpus: int | None = None, mem_gib: float | None = None) -> int:
    """TIME-21: how many counting processes a sizing on a machine of its own may use: its CPU quota (as the snappy
    ranks and the cfMesh threads read it: engines/determinism.job_cpus), at most as many as its memory holds
    (MEMORY_SHARE of it, less PARENT_GIB, CHILD_GIB each) and at most VM_MAX_WORKERS - never fewer than the worker's
    own (MAX_WORKERS, or its CPUs when fewer). c2d-standard-32 (32 vCPUs, 128 GB): 32; n2-highmem-16: 16."""
    from meshpipeline.engines.determinism import job_cpus
    cpus = job_cpus() if cpus is None else int(cpus)
    mem = memory_gib() if mem_gib is None else float(mem_gib)
    by_memory = int((mem * MEMORY_SHARE - PARENT_GIB) // CHILD_GIB)
    return max(1, min(cpus, max(min(MAX_WORKERS, cpus), min(VM_MAX_WORKERS, by_memory))))


def plan_workers() -> int:
    """How many processes the histogram may use: the job's CPU quota (never the host's cores), at
    most MAX_WORKERS - or, on a sizing VM (set_workers_cap, TIME-21), at most its cap."""
    from meshpipeline.engines.determinism import job_cpus
    return max(1, min(_WORKERS_CAP or MAX_WORKERS, job_cpus()))


class _Pool:
    """`n` child processes (`python -m meshpipeline.engines.fea_size_field`), each holding the same
    field, measuring slices of an octree level. One thread per child sends a slice and reads its
    answer (pickles over the child's pipes), so no pipe fills while another waits."""

    def __init__(self, spec, n: int):
        import subprocess
        import sys
        from concurrent.futures import ThreadPoolExecutor

        self.n = int(n)
        self._threads: ThreadPoolExecutor | None = None
        self._sent: dict | None = None
        self.procs = [subprocess.Popen([sys.executable, "-m", "meshpipeline.engines.fea_size_field"],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE) for _ in range(self.n)]
        self.init(spec)

    def init_message(self, spec) -> tuple:
        """What tells the children this field: the zones, and the surface only when it is not the one they hold
        (TIME-12: a sizing's passes share one surface; it was sent to every child for every pass)."""
        surf, sent = spec[0], self._sent
        if sent is not None and all(surf[k] is sent[k] for k in ("xyz", "tris", "label")) \
                and surf["names"] == sent["names"]:
            return ("init", (None, *spec[1:]))
        self._sent = surf
        return ("init", spec)

    def init(self, spec) -> None:
        """Each child builds this field (a child told a new one keeps the last one's surface distances)."""
        import pickle
        data = pickle.dumps(self.init_message(spec), protocol=pickle.HIGHEST_PROTOCOL)
        for proc in self.procs:
            proc.stdin.write(data)  # type: ignore[union-attr]
            proc.stdin.flush()  # type: ignore[union-attr]

    def _ask(self, proc, job):
        import pickle
        pickle.dump(job, proc.stdin, protocol=pickle.HIGHEST_PROTOCOL)
        proc.stdin.flush()
        ok, value = pickle.load(proc.stdout)  # noqa: S301 - our own child, over its own pipe
        if not ok:
            raise RuntimeError(f"a size-field worker failed: {value}")
        return value

    def map(self, what: str, chunks: list, r: float) -> list:
        from concurrent.futures import ThreadPoolExecutor

        out: list = [None] * len(chunks)

        def run(k: int) -> None:
            proc = self.procs[k]
            for i in range(k, len(chunks), self.n):
                out[i] = self._ask(proc, (what, chunks[i], r))

        # one set of threads for the pool's life (TIME-12): they only wait on the children's pipes
        if self._threads is None:
            self._threads = ThreadPoolExecutor(max_workers=self.n)
        list(self._threads.map(run, range(self.n)))
        return out

    def close(self) -> None:
        import pickle
        if self._threads is not None:
            self._threads.shutdown(wait=True)
            self._threads = None
        for proc in self.procs:
            try:
                pickle.dump(("stop", None), proc.stdin)  # type: ignore[arg-type]
                proc.stdin.close()  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001 - a child already gone is closed
                proc.kill()
        for proc in self.procs:
            try:
                proc.wait(timeout=30)
            except Exception:  # noqa: BLE001 - one that will not stop is killed
                proc.kill()


def _serve() -> None:
    """A child of _Pool: build the field, then answer each slice until told to stop (told a new field within a sizing,
    it builds that one, keeping the last one's surface distances). Its answers go on a private copy of stdout;
    anything else written to stdout goes to stderr."""
    import os
    import pickle
    import sys
    from types import SimpleNamespace

    out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    _scope().depth = 1                  # the child lives for one pool: its fields share their surface distances
    field: SizeField | None = None
    held: dict | None = None            # the surface it was last sent (a pass told only its zones keeps it)
    src = sys.stdin.buffer
    while True:
        try:
            what, arg, *rest = pickle.load(src)  # noqa: S301 - our own parent, over its own pipe
        except EOFError:
            return
        if what == "stop":
            return
        try:
            if what == "init":
                surf, zones, growth, scale = arg
                surf = held = held if surf is None else surf
                assert surf is not None
                field = None            # the last field goes first (its surface distances stay, in the scope)
                field = SizeField(SimpleNamespace(**surf), zones, growth=growth, scale_to_m=scale)
                continue
            assert field is not None
            r = rest[0] if rest else 0.0
            value = field.signed(arg) if what == "signed" else field.sizes(arg, r)
            pickle.dump((True, value), out, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception as exc:  # noqa: BLE001 - told to the parent, which raises it
            pickle.dump((False, f"{type(exc).__name__}: {exc}"), out)
        out.flush()


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


def elements(hist: Histogram | list, base: float, per_h3: float, surface=None) -> float:
    """The element estimate at base size `base` (metres): the volume's count, plus PLAN-fea-31's thin walls'
    surface count past their share of it (`surface`: the rows, when `hist` is a bare list)."""
    rows = hist.rows if isinstance(hist, Histogram) else Histogram.from_list(hist).rows
    srf = (hist.surface if isinstance(hist, Histogram) else surface) or []
    n = per_h3 * sum(v / min(h, base) ** 3 for h, v in rows)
    for h, t, a in srf:
        hh = min(float(h), float(base))
        n += float(a) / hh ** 2 * surface_share(float(t), hh, per_h3)
    return float(n)


def surface_share(t: float, h: float, per_h3: float) -> float:
    """PLAN-fea-31: elements per area / h^2 a surface adds to the volume's count: the layer every surface triangle
    carries (SURFACE_PER_AREA), or on a wall thinner than the size what the wall holds past its share of the
    volume, whichever is more."""
    slab = SLAB_PER_AREA - 0.5 * per_h3 * t / h if t > 0 else 0.0
    return max(SURFACE_PER_AREA, slab)


__all__ = ["Histogram", "SizeField", "elements", "group_zones", "sizing"]


if __name__ == "__main__":
    _serve()
