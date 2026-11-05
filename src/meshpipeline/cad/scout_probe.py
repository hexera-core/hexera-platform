# Responsibility: Answer the scout's two geometric questions about a CAD part - is this point in
# the part's material, and where does this straight line meet the part - fast, and with the answers
# the exact B-rep gives.
# Boundaries: geometry queries on one already-meshed OpenCascade shape. It decides nothing about
# openings or bodies; cad/scout asks, and reads the answers exactly as it read OpenCascade's.
#
# WHY: OpenCascade's exact point classifier (BRepClass3d_SolidClassifier) costs 15-600 ms a point
# on a part with a few hundred curved faces - it cuts its test rays against every face of the solid
# - and the scout asks hundreds to tens of thousands of points (every point, of every solid). On the
# Toyota Supra (62 solids) it was 633 of 669 s; on a 3D-printed boat, 186 of 206 s.
#
# HOW, without changing an answer. A point is answered by the first of three that can:
#   1. THE MESH. The part is already meshed (the scout reads its rims off that mesh). How far the
#      mesh strays from the true surface is measured face by face: the mesher's own figure for a
#      face's interior, and every rim segment against the true edge curve. A point farther from the
#      mesh than twice that stray is on the same side of the true surface as of the mesh, so the
#      mesh decides, by the classifier's own rule (the side the nearest crossing of a ray faces),
#      checked against the crossings' parity, on two rays through a spatial index that must agree.
#   2. EXACT RAYS. Near the surface, rays are cut exactly by OpenCascade against the true faces -
#      only those whose mesh the ray passes near, nearest first, stopping at the nearest crossing -
#      and the nearest crossing's side decides. Two readable rays must agree; a crossing on an edge,
#      at a graze or on the surface itself is not read.
#   3. THE CLASSIFIER, for whatever is left, and for any solid whose mesh cannot stand for it (an
#      open shell, an internal face, an inside-out solid).
# Lines (clear ahead, depth) are cut exactly too, face by face nearest first, against only the
# faces whose mesh the line passes near - the only faces it can meet.
from __future__ import annotations

import logging
import math
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

#: How far from a face's mesh a point must be, in multiples of how far that mesh strays from the
#: true face (plus twice the part's largest tolerance), before the mesh's side of the surface is
#: taken as the true surface's side.
NEAR_STRAYS = 2.0
#: A crossing this close (in barycentric terms) to a triangle's edge, or a ray this close to
#: parallel with a triangle, is not counted: another ray is tried.
EDGE_EPS = 1e-7
#: An exact crossing whose ray meets the face at a smaller sine than this is a graze, not a
#: crossing that can be read: near a tangency a cutter can find one root of a close pair.
GRAZE_SINE = 1e-3
#: Generic ray directions (none along an axis or in an axis plane), tried in turn.
_DIRECTIONS = tuple(
    tuple(c / math.sqrt(sum(v * v for v in d)) for c in d)
    for d in ((1.0, 0.6180339887, 0.3819660113), (-0.4142135624, 1.0, 0.7320508076),
              (0.2679491924, -0.5857864376, 1.0), (-0.7071067812, -0.3090169944, -1.0)))

#: per-solid answers
_OUT, _IN, _UNSURE = 0, 1, -1


# ------------------------------------------------------------------- reading the mesh ----
class _FaceMesh:
    """One face's triangles (wound so their normal points out of the material), how far its
    interior strays from the true face, and its rim segments with how far each strays from the
    true edge (None when the rim could not be held to its edges)."""

    __slots__ = ("tris", "interior", "rim")

    def __init__(self, tris: np.ndarray, interior: float, rim: tuple[np.ndarray, np.ndarray, np.ndarray] | None):
        self.tris, self.interior, self.rim = tris, interior, rim


def _face_mesh(face) -> _FaceMesh | None:
    """The face's triangulation read into numpy, or None when the mesher left the face bare."""
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_REVERSED
    from OCP.TopLoc import TopLoc_Location

    loc = TopLoc_Location()
    tri = BRep_Tool.Triangulation_s(face, loc)
    if tri is None or tri.NbTriangles() == 0 or tri.NbNodes() == 0:
        return None
    nodes = np.array([tri.Node(i).Coord() for i in range(1, tri.NbNodes() + 1)], dtype=float)
    if not loc.IsIdentity():
        t = loc.Transformation()
        rot = np.array([[t.Value(r, c) for c in (1, 2, 3)] for r in (1, 2, 3)], dtype=float)
        shift = np.array([t.Value(r, 4) for r in (1, 2, 3)], dtype=float)
        nodes = nodes @ rot.T + shift
    idx = np.array([tri.Triangle(k).Get() for k in range(1, tri.NbTriangles() + 1)], dtype=np.int64) - 1
    if face.Orientation() == TopAbs_REVERSED:
        idx = idx[:, ::-1]
    return _FaceMesh(nodes[idx], float(tri.Deflection()), _rim_segments(face, tri, loc, nodes))


#: Where along each rim segment its stray from the true edge is measured: the middle, where a
#: smooth curve bows furthest from its chord, and the quarter points either side, where a curve
#: whose bend is not centred on the segment bows furthest.
_RIM_SAMPLES = (0.25, 0.5, 0.75)


def _rim_segments(face, tri, loc, nodes: np.ndarray):
    """(starts, ends, strays) of every segment of the face's rim polygons, each stray the farthest
    the true edge curve lies from the segment at _RIM_SAMPLES of its parameter span; None when an
    edge's polygon carries no parameters or the edge has no curve to hold it to."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    starts, ends, strays = [], [], []
    exp = TopExp_Explorer(face, TopAbs_EDGE)
    while exp.More():
        edge = TopoDS.Edge_s(exp.Current())
        exp.Next()
        if BRep_Tool.Degenerated_s(edge):
            continue
        pol = BRep_Tool.PolygonOnTriangulation_s(edge, tri, loc)
        if pol is None or not pol.HasParameters():
            return None
        n = pol.NbNodes()
        if n < 2:
            continue
        ids, prm = pol.Nodes(), pol.Parameters()
        pts = nodes[[ids.Value(ids.Lower() + i) - 1 for i in range(n)]]
        ts = [prm.Value(prm.Lower() + i) for i in range(n)]
        stray = np.zeros(n - 1)
        try:
            curve = BRepAdaptor_Curve(edge)
            for f in _RIM_SAMPLES:
                on_curve = np.array([curve.Value((1.0 - f) * ts[i] + f * ts[i + 1]).Coord() for i in range(n - 1)],
                                    dtype=float)
                stray = np.maximum(stray, np.linalg.norm(on_curve - ((1.0 - f) * pts[:-1] + f * pts[1:]), axis=1))
        except Exception:  # noqa: BLE001 - an edge with no 3D curve to hold the polygon to
            return None
        starts.append(pts[:-1])
        ends.append(pts[1:])
        strays.append(stray)
    if not starts:
        return np.zeros((0, 3)), np.zeros((0, 3)), np.zeros(0)
    return np.concatenate(starts), np.concatenate(ends), np.concatenate(strays)


# ---------------------------------------------------------------------------- geometry ----
def _point_triangle_distance(p: np.ndarray, tris: np.ndarray) -> np.ndarray:
    """Distance from one point to each triangle (n, 3, 3)."""
    return np.linalg.norm(_closest_on_triangles(p, tris) - p, axis=1)


def _closest_on_triangles(p: np.ndarray, tris: np.ndarray) -> np.ndarray:
    """The closest point to `p` on each triangle (n, 3, 3), by the triangle's Voronoi regions
    (Ericson, Real-Time Collision Detection, 5.1.5), vectorised. A degenerate (zero-area) triangle
    gets the closest point of its three edges."""
    a, b, c = tris[:, 0], tris[:, 1], tris[:, 2]
    ab, ac, ap = b - a, c - a, p - a
    d1 = np.einsum("ij,ij->i", ab, ap)
    d2 = np.einsum("ij,ij->i", ac, ap)
    bp = p - b
    d3 = np.einsum("ij,ij->i", ab, bp)
    d4 = np.einsum("ij,ij->i", ac, bp)
    cp = p - c
    d5 = np.einsum("ij,ij->i", ab, cp)
    d6 = np.einsum("ij,ij->i", ac, cp)
    va = d3 * d6 - d5 * d4
    vb = d5 * d2 - d1 * d6
    vc = d1 * d4 - d3 * d2
    with np.errstate(divide="ignore", invalid="ignore"):
        denom = va + vb + vc
        v_in = vb / denom
        w_in = vc / denom
        t_ab = d1 / (d1 - d3)
        t_ac = d2 / (d2 - d6)
        t_bc = (d4 - d3) / ((d4 - d3) + (d5 - d6))
        closest = a + v_in[:, None] * ab + w_in[:, None] * ac          # over the face itself
        # the edge and corner regions, the earliest test in Ericson's order applied last so it wins
        r_bc = (va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0)
        closest = np.where(r_bc[:, None], b + t_bc[:, None] * (c - b), closest)
        r_ac = (vb <= 0) & (d2 >= 0) & (d6 <= 0)
        closest = np.where(r_ac[:, None], a + t_ac[:, None] * ac, closest)
        r_c = (d6 >= 0) & (d5 <= d6)
        closest = np.where(r_c[:, None], c, closest)
        r_ab = (vc <= 0) & (d1 >= 0) & (d3 <= 0)
        closest = np.where(r_ab[:, None], a + t_ab[:, None] * ab, closest)
        r_b = (d3 >= 0) & (d4 <= d3)
        closest = np.where(r_b[:, None], b, closest)
        r_a = (d1 <= 0) & (d2 <= 0)
        closest = np.where(r_a[:, None], a, closest)
    bad = ~np.all(np.isfinite(closest), axis=1)
    if np.any(bad):
        cands = np.stack([_closest_on_segments(p, tris[bad][:, i], tris[bad][:, j])
                          for i, j in ((0, 1), (1, 2), (2, 0))], axis=1)            # (m, 3, 3)
        pick = np.argmin(np.linalg.norm(cands - p, axis=2), axis=1)
        closest[bad] = cands[np.arange(len(pick)), pick]
    return closest


def _closest_on_segments(p: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    ab = b - a
    ll = np.einsum("ij,ij->i", ab, ab)
    t = np.clip(np.einsum("ij,ij->i", p - a, ab) / np.where(ll > 0, ll, 1.0), 0.0, 1.0)
    return a + t[:, None] * ab


def _point_segment_distance(p: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return np.linalg.norm(_closest_on_segments(p, a, b) - p, axis=1)


def _exact_box(face) -> tuple[np.ndarray, np.ndarray]:
    """A face's own box from its exact geometry, for a face that has no mesh to read it off."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(face, box, False, False)
    if box.IsVoid():
        return np.full(3, -np.inf), np.full(3, np.inf)
    x0, y0, z0, x1, y1, z1 = box.Get()
    return np.array([x0, y0, z0]), np.array([x1, y1, z1])


def _signed_volume(tris: np.ndarray) -> float:
    return float(np.einsum("ij,ij->i", tris[:, 0], np.cross(tris[:, 1], tris[:, 2])).sum() / 6.0)


def _slab(o: np.ndarray, d: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """For each box (lo, hi), the stretch [t_in, t_out] of the line o + t d inside it (empty when
    t_in > t_out)."""
    par = np.abs(d) < 1e-15
    inside = (o >= lo) & (o <= hi)
    with np.errstate(divide="ignore", invalid="ignore"):
        t1 = (lo - o) / np.where(par, 1.0, d)
        t2 = (hi - o) / np.where(par, 1.0, d)
    t_in = np.where(par, np.where(inside, -np.inf, np.inf), np.minimum(t1, t2)).max(axis=1)
    t_out = np.where(par, np.where(inside, np.inf, -np.inf), np.maximum(t1, t2)).min(axis=1)
    return t_in, t_out


def _first_per_group(groups: np.ndarray, values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(group, index of its smallest value) for each group present."""
    order = np.lexsort((values, groups))
    g = groups[order]
    first = np.ones(len(order), dtype=bool)
    first[1:] = g[1:] != g[:-1]
    return g[first], order[first]


def _line(o, d):
    from OCP.gp import gp_Dir, gp_Lin, gp_Pnt

    return gp_Lin(gp_Pnt(float(o[0]), float(o[1]), float(o[2])), gp_Dir(float(d[0]), float(d[1]), float(d[2])))


def _cut(shape, o, d) -> list[float]:
    """Every parameter at which the line meets the shape's faces (BRepIntCurveSurface_Inter)."""
    from OCP.BRepIntCurveSurface import BRepIntCurveSurface_Inter

    inter = BRepIntCurveSurface_Inter()
    inter.Init(shape, _line(o, d), 1e-9)
    out = []
    while inter.More():
        out.append(float(inter.W()))
        inter.Next()
    return out


def _vtk_locator(cells: np.ndarray, *, boxes: bool = True):
    """(polydata, locator) over cells of 3 (triangles) or 2 (segments) points each: (n, k, 3).
    For box questions a cell tree, which tests each cell's own bounds (a 1.8 m rig's box question
    met 5 cells, against 1,600 from the bucket grid); for line questions a bucket grid one cell
    to a bucket, which walks a line fastest."""
    import vtk
    from vtkmodules.util import numpy_support

    n, k = cells.shape[0], cells.shape[1]
    pts = vtk.vtkPoints()
    pts.SetData(numpy_support.numpy_to_vtk(np.ascontiguousarray(cells.reshape(-1, 3)), deep=True))
    arr = vtk.vtkCellArray()
    arr.SetData(numpy_support.numpy_to_vtkIdTypeArray(np.arange(0, k * n + 1, k, dtype=np.int64), deep=True),
                numpy_support.numpy_to_vtkIdTypeArray(np.arange(k * n, dtype=np.int64), deep=True))
    poly = vtk.vtkPolyData()
    poly.SetPoints(pts)
    if k == 3:
        poly.SetPolys(arr)
    else:
        poly.SetLines(arr)
    loc = vtk.vtkCellTreeLocator() if boxes else vtk.vtkStaticCellLocator()
    if not boxes:
        loc.SetNumberOfCellsPerNode(1)
    loc.SetDataSet(poly)
    loc.BuildLocator()
    return poly, loc


def _box_locator(poly):
    """A cell tree over cells already in `poly`, for box questions."""
    import vtk

    loc = vtk.vtkCellTreeLocator()
    loc.SetDataSet(poly)
    loc.BuildLocator()
    return loc


def _ids(id_list) -> np.ndarray:
    n = id_list.GetNumberOfIds()
    return np.fromiter((id_list.GetId(i) for i in range(n)), dtype=np.int64, count=n)


def _cube(p, r) -> list[float]:
    return [p[0] - r, p[0] + r, p[1] - r, p[1] + r, p[2] - r, p[2] + r]


class _ReachIndex:
    """Cells (triangles or segments), each with its own reach: which lie within their reach of a
    point. Cells are binned by reach, each bin with its own index asked out to that bin's widest
    reach, so a few far-reaching cells (a rim that bulges 40 mm) do not widen every question."""

    def __init__(self, cells: np.ndarray, reach: np.ndarray, *, base: Any = None) -> None:
        self.cells, self.reach = cells, reach
        self._dist = _point_triangle_distance if cells.shape[1] == 3 else (
            lambda p, c: _point_segment_distance(p, c[:, 0], c[:, 1]))
        # most cells: one index (the caller's own, when it has one over the same cells), asked out
        # to the 95th-percentile reach; the rest in bins of fourfold reach
        self.r0 = float(np.quantile(reach, 0.95)) if len(reach) else 0.0
        self._bins: list[tuple[float, Any, np.ndarray, Any]] = []
        self._base: tuple[Any, Any] = base if base is not None else (_vtk_locator(cells) if len(cells) else (None, None))
        rest = np.flatnonzero(reach > self.r0)
        lo = self.r0
        while len(rest):
            hi = 4.0 * lo if lo > 0 else float(reach[rest].max())
            take = rest[reach[rest] <= hi] if lo > 0 else rest
            if len(take) == 0:
                lo = hi
                continue
            poly, loc = _vtk_locator(cells[take])
            self._bins.append((float(reach[take].max()), loc, take, poly))
            rest = rest[reach[rest] > hi] if lo > 0 else rest[:0]
            lo = hi

    def in_cube(self, p, r: float) -> np.ndarray:
        """The cells whose bounds meet the cube of half-side r about p, whatever their reach."""
        import vtk

        loc = self._base[1]
        if loc is None:
            return np.zeros(0, dtype=np.int64)
        ids = vtk.vtkIdList()
        loc.FindCellsWithinBounds(_cube(p, r), ids)
        return _ids(ids)

    def within(self, p) -> tuple[np.ndarray, np.ndarray]:
        """(cell ids, distances) of the cells within their own reach of `p`."""
        import vtk

        found = []
        loc = self._base[1]
        if loc is not None:
            ids = vtk.vtkIdList()
            loc.FindCellsWithinBounds(_cube(p, self.r0), ids)
            found.append(_ids(ids))
        for r, loc_b, take, _poly in self._bins:
            ids = vtk.vtkIdList()
            loc_b.FindCellsWithinBounds(_cube(p, r), ids)
            got = _ids(ids)
            if len(got):
                found.append(take[got])
        c = np.unique(np.concatenate(found)) if found else np.zeros(0, dtype=np.int64)
        if len(c) == 0:
            return c, np.zeros(0)
        d = self._dist(np.asarray(p, dtype=float), self.cells[c])
        keep = d <= self.reach[c]
        return c[keep], d[keep]


# -------------------------------------------------------------------------- the probes ----
class ExactProbe:
    """The scout's questions answered the exact, slow way - each solid's own classifier, the line
    cut against every face of the shape. The reference the fast answers keep, and the fallback when
    the mesh index cannot be built."""

    def __init__(self, shape, solids: list) -> None:
        self._shape = shape
        self._solids = list(solids)
        self._classifiers: dict[int, Any] = {}
        self.n_mesh = self.n_ray = self.n_classifier = 0

    def _classify(self, s_i: int, p) -> bool:
        from OCP.BRepClass3d import BRepClass3d_SolidClassifier
        from OCP.gp import gp_Pnt
        from OCP.TopAbs import TopAbs_IN

        c = self._classifiers.get(s_i)
        if c is None:
            c = self._classifiers[s_i] = BRepClass3d_SolidClassifier(self._solids[s_i])
        c.Perform(gp_Pnt(*p), 1e-9)
        return c.State() == TopAbs_IN

    def inside_any_exact(self, p) -> bool:
        """What the classifiers say, every solid in turn."""
        p = (float(p[0]), float(p[1]), float(p[2]))
        return any(self._classify(k, p) for k in range(len(self._solids)))

    def meets_beyond_exact(self, origin, direction, beyond: float) -> bool:
        return any(w > beyond for w in _cut(self._shape, np.asarray(origin, float), np.asarray(direction, float)))

    def first_beyond_exact(self, origin, direction, beyond: float) -> float:
        return min((w for w in _cut(self._shape, np.asarray(origin, float), np.asarray(direction, float))
                    if w > beyond), default=0.0)

    def inside_any(self, p) -> bool:
        self.n_classifier += 1
        return self.inside_any_exact(p)

    def meets_beyond(self, origin, direction, beyond: float) -> bool:
        return self.meets_beyond_exact(origin, direction, beyond)

    def first_beyond(self, origin, direction, beyond: float) -> float:
        return self.first_beyond_exact(origin, direction, beyond)


def make_probe(shape, solids: list, *, deflection: float) -> ExactProbe:
    """The fast probe for a meshed shape - or, should its mesh index fail to build, the exact one:
    the index only speeds the answers up, it is never needed for them."""
    try:
        return PartProbe(shape, solids, deflection=deflection)
    except Exception as exc:  # noqa: BLE001 - an accelerator that fails leaves the exact answers
        logger.warning("scout probe: the mesh index could not be built (%s: %s); answering exactly",
                       type(exc).__name__, str(exc)[:200])
        return ExactProbe(shape, solids)


class LazyProbe:
    """A probe built on the first question: a part the scout asks nothing (no flat faces to probe)
    pays nothing for the index."""

    def __init__(self, shape, solids: list, *, deflection: float) -> None:
        self._args = (shape, solids, deflection)
        self._probe: ExactProbe | None = None

    def _get(self) -> ExactProbe:
        if self._probe is None:
            shape, solids, deflection = self._args
            self._probe = make_probe(shape, solids, deflection=deflection)
        return self._probe

    def inside_any(self, p) -> bool:
        return self._get().inside_any(p)

    def meets_beyond(self, origin, direction, beyond: float) -> bool:
        return self._get().meets_beyond(origin, direction, beyond)

    def first_beyond(self, origin, direction, beyond: float) -> float:
        return self._get().first_beyond(origin, direction, beyond)


class PartProbe(ExactProbe):
    """Point-in-material and line cuts for a meshed shape and its solids.

    `inside_any(p)` is True when `p` is IN one of `solids` - what asking each solid's
    BRepClass3d_SolidClassifier (tolerance 1e-9) says. `meets_beyond` / `first_beyond` say whether,
    and where first, the line meets the shape's faces past a parameter - what
    BRepIntCurveSurface_Inter over the whole shape finds. The counters say how many points the mesh
    answered (`n_mesh`), how many exact rays (`n_ray`), and how many the classifier
    (`n_classifier`)."""

    def __init__(self, shape, solids: list, *, deflection: float) -> None:
        from OCP.BRep import BRep_Tool
        from OCP.ShapeAnalysis import ShapeAnalysis_ShapeTolerance
        from OCP.TopAbs import TopAbs_EXTERNAL, TopAbs_FACE, TopAbs_INTERNAL, TopAbs_SHELL
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopoDS import TopoDS
        from OCP.TopTools import TopTools_IndexedMapOfShape

        super().__init__(shape, solids)
        self._cutters: dict[tuple[int, int], tuple] = {}

        # every face of the shape once, with its mesh
        fmap = TopTools_IndexedMapOfShape()
        exp = TopExp_Explorer(shape, TopAbs_FACE)
        while exp.More():
            fmap.Add(exp.Current())
            exp.Next()
        self._faces = [TopoDS.Face_s(fmap.FindKey(i)) for i in range(1, fmap.Extent() + 1)]
        nf = len(self._faces)
        meshes = [_face_mesh(f) for f in self._faces]
        self._tol = float(ShapeAnalysis_ShapeTolerance().Tolerance(shape, 1)) if nf else 0.0
        slack = 2.0 * self._tol

        # HOW FAR EACH FACE'S MESH STRAYS. Inside a face, what the mesher measured; along its rim,
        # each polygon segment against the true edge - a segment that strays further than the
        # interior does becomes a capsule of its own, so one bulging edge does not widen the whole
        # face. A rim that cannot be measured falls back to the deflection asked for, face-wide.
        self._face_margin = np.zeros(nf)
        cap_a, cap_b, cap_r, cap_face = [], [], [], []
        for i, m in enumerate(meshes):
            if m is None:
                continue
            if m.rim is None:
                self._face_margin[i] = NEAR_STRAYS * max(m.interior, float(deflection)) + slack
                continue
            self._face_margin[i] = NEAR_STRAYS * m.interior + slack
            a, b, stray = m.rim
            wide = NEAR_STRAYS * stray + slack > self._face_margin[i]
            if np.any(wide):
                cap_a.append(a[wide])
                cap_b.append(b[wide])
                cap_r.append(NEAR_STRAYS * stray[wide] + slack)
                cap_face.append(np.full(int(wide.sum()), i, dtype=np.int64))
        if cap_a:
            self._caps = np.stack([np.concatenate(cap_a), np.concatenate(cap_b)], axis=1)     # (n, 2, 3)
            self._cap_r, self._cap_face = np.concatenate(cap_r), np.concatenate(cap_face)
        else:
            self._caps, self._cap_r, self._cap_face = np.zeros((0, 2, 3)), np.zeros(0), np.zeros(0, dtype=np.int64)
        self._face_cap_reach = np.zeros(nf)
        if len(self._cap_r):
            np.maximum.at(self._face_cap_reach, self._cap_face, self._cap_r)

        # EACH FACE'S REACH, as balls: its triangles' circumscribing balls grown by its margin,
        # and its capsules' balls. A line outside every ball of a face cannot meet the face. A
        # face the mesher left bare is a hole in the mesh: its exact box stands in, and no mesh
        # answer may look through it.
        ball_c, ball_r, ball_f = [], [], []
        lo, hi = np.zeros((nf, 3)), np.zeros((nf, 3))
        self._bare = np.zeros(nf, dtype=bool)
        for i, m in enumerate(meshes):
            if m is None:
                self._bare[i] = True
                lo[i], hi[i] = _exact_box(self._faces[i])
                lo[i] -= slack
                hi[i] += slack
                continue
            c = m.tris.mean(axis=1)
            r = np.linalg.norm(m.tris - c[:, None, :], axis=2).max(axis=1) + self._face_margin[i]
            ball_c.append(c)
            ball_r.append(r)
            ball_f.append(np.full(len(c), i, dtype=np.int64))
            lo[i], hi[i] = (c - r[:, None]).min(axis=0), (c + r[:, None]).max(axis=0)
        if len(self._cap_r):
            mid = self._caps.mean(axis=1)
            r = 0.5 * np.linalg.norm(self._caps[:, 1] - self._caps[:, 0], axis=1) + self._cap_r
            ball_c.append(mid)
            ball_r.append(r)
            ball_f.append(self._cap_face)
            np.minimum.at(lo, self._cap_face, mid - r[:, None])
            np.maximum.at(hi, self._cap_face, mid + r[:, None])
        # the balls sorted by face, with each face's run, so a question reads only its faces' balls
        bf = np.concatenate(ball_f) if ball_f else np.zeros(0, dtype=np.int64)
        by_face = np.argsort(bf, kind="stable")
        self._ball_c = (np.concatenate(ball_c) if ball_c else np.zeros((0, 3)))[by_face]
        self._ball_r = (np.concatenate(ball_r) if ball_r else np.zeros(0))[by_face]
        self._ball_f = bf[by_face]
        self._ball_start = np.searchsorted(self._ball_f, np.arange(nf), side="left")
        self._ball_end = np.searchsorted(self._ball_f, np.arange(nf), side="right")
        self._face_lo, self._face_hi = lo, hi
        bare_ids = np.flatnonzero(self._bare)
        self._hole_lo, self._hole_hi = lo[bare_ids], hi[bare_ids]

        # each solid's own triangles, tagged with the solid, and its faces as the solid holds
        # them. A solid whose mesh cannot stand for it is always answered by the classifier: a face
        # that is not a boundary (internal/external), a shell that does not close, or a mesh that
        # encloses negative volume (inside out).
        tris, owner, margins, tri_face = [], [], [], []
        self._face_in_solid = np.zeros((len(self._solids), nf), dtype=bool)
        self._trusted: list[bool] = []
        self._solid_lo: list[np.ndarray] = []
        self._solid_hi: list[np.ndarray] = []
        self._solid_faces: list[np.ndarray] = []
        self._occurrence: list[dict[int, Any]] = []
        for s_i, solid in enumerate(self._solids):
            ok = True
            parts, part_margin, part_face, f_idx = [], [], [], []
            occ: dict[int, Any] = {}
            fe = TopExp_Explorer(solid, TopAbs_FACE)
            while fe.More():
                face = TopoDS.Face_s(fe.Current())
                fe.Next()
                k = fmap.FindIndex(face) - 1
                if k < 0:
                    ok = False
                    continue
                if face.Orientation() in (TopAbs_INTERNAL, TopAbs_EXTERNAL):
                    ok = False
                m = meshes[k]
                if m is not None:
                    parts.append(m.tris if face.Orientation() == self._faces[k].Orientation() else m.tris[:, ::-1])
                    part_margin.append(np.full(len(m.tris), self._face_margin[k]))
                    part_face.append(np.full(len(m.tris), k, dtype=np.int64))
                if k not in occ:
                    f_idx.append(k)
                    occ[k] = face
            fi = np.asarray(f_idx, dtype=np.int64)
            self._solid_faces.append(fi)
            self._occurrence.append(occ)
            self._face_in_solid[s_i, fi] = True
            shells = 0
            se = TopExp_Explorer(solid, TopAbs_SHELL)
            while se.More():
                shells += 1
                ok = ok and BRep_Tool.IsClosed_s(se.Current())
                se.Next()
            if parts:
                arr = np.concatenate(parts)
                pts = arr.reshape(-1, 3)
                ok = ok and shells > 0 and _signed_volume(arr - 0.5 * (pts.min(axis=0) + pts.max(axis=0))) > 0.0
                tris.append(arr)
                owner.append(np.full(len(arr), s_i, dtype=np.int64))
                margins.append(np.concatenate(part_margin))
                tri_face.append(np.concatenate(part_face))
            else:
                ok = False
            # the solid reaches as far as its faces' reach
            if len(fi):
                self._solid_lo.append(self._face_lo[fi].min(axis=0))
                self._solid_hi.append(self._face_hi[fi].max(axis=0))
            else:
                self._solid_lo.append(np.full(3, -np.inf))
                self._solid_hi.append(np.full(3, np.inf))
            self._trusted.append(ok)

        self._locator: Any = None
        self._size = 1.0
        if tris:
            self._tris = np.concatenate(tris)
            self._owner = np.concatenate(owner)
            self._tri_margin = np.concatenate(margins)
            self._tri_face = np.concatenate(tri_face)
            pts = self._tris.reshape(-1, 3)
            self._size = float(np.linalg.norm(pts.max(axis=0) - pts.min(axis=0)))
            self._reach = 2.0 * self._size + 1.0
            self._poly, self._locator = _vtk_locator(self._tris, boxes=False)       # for rays
            self._box_locator = _box_locator(self._poly)                           # for boxes
            self._v0 = self._tris[:, 0]
            self._e1 = self._tris[:, 1] - self._tris[:, 0]
            self._e2 = self._tris[:, 2] - self._tris[:, 0]
            self._nrm = np.cross(self._e1, self._e2)
            self._area2 = np.linalg.norm(self._nrm, axis=1)
            self._near_tris = _ReachIndex(self._tris, self._tri_margin, base=(self._poly, self._box_locator))
            self._near_caps = _ReachIndex(self._caps, self._cap_r) if len(self._cap_r) else None
            self._typical = float(np.median(self._tri_margin))

    # -------------------------------------------------------------------- near the mesh ----
    def _near(self, p) -> dict[int, int]:
        """The solids `p` lies near - within a face's margin of one of its triangles, within a
        capsule's reach of a rim segment that strays, or inside the box of a face the mesher left
        bare - each with the nearest of its triangles that is in reach (-1 when none is)."""
        pv = np.asarray(p, dtype=float)
        out: dict[int, int] = {}
        if len(self._hole_lo):
            inside = np.all((pv >= self._hole_lo) & (pv <= self._hole_hi), axis=1)
            holes = np.flatnonzero(self._bare)[inside]
            for s_i in np.flatnonzero(self._face_in_solid[:, holes].any(axis=1)).tolist():
                out[s_i] = -1
        if self._near_caps is not None:
            c, _d = self._near_caps.within(p)
            if len(c):
                for s_i in np.flatnonzero(self._face_in_solid[:, self._cap_face[c]].any(axis=1)).tolist():
                    out[s_i] = -1
        c, d = self._near_tris.within(p)
        if len(c):
            solids, at = _first_per_group(self._owner[c], d)
            for s_i, j in zip(solids.tolist(), at.tolist(), strict=True):
                out[s_i] = int(c[j])
        # a solid near only by a capsule or a bare face's box: aim at its nearest triangle about
        if any(v < 0 for v in out.values()):
            import vtk

            ids = vtk.vtkIdList()
            self._box_locator.FindCellsWithinBounds(_cube(p, 2.0 * self._near_tris.r0), ids)
            c = _ids(ids)
            if len(c):
                want = np.isin(self._owner[c], [s for s, v in out.items() if v < 0])
                c = c[want]
            if len(c):
                d = _point_triangle_distance(pv, self._tris[c])
                solids, at = _first_per_group(self._owner[c], d)
                for s_i, j in zip(solids.tolist(), at.tolist(), strict=True):
                    out[s_i] = int(c[j])
        return out

    # ---------------------------------------------------------------------- mesh rays ----
    def _mesh_ray(self, p, d) -> np.ndarray | None:
        """Per solid, what the mesh says along the ray from `p` along `d`: IN, OUT or UNSURE (the
        parity of its crossings and the side its nearest crossing faces disagree). None when the
        ray grazes a triangle's edge, runs in a triangle's plane, or passes a bare face's box, so
        its crossings cannot be counted."""
        import vtk

        o = np.asarray(p, dtype=float)
        dv = np.asarray(d, dtype=float)
        if len(self._hole_lo):
            t_in, t_out = _slab(o, dv, self._hole_lo, self._hole_hi)
            if np.any((t_in <= t_out) & (t_out >= 0.0)):
                return None
        q = [p[0] + self._reach * d[0], p[1] + self._reach * d[1], p[2] + self._reach * d[2]]
        ids = vtk.vtkIdList()
        self._locator.FindCellsAlongLine(list(p), q, 0.0, ids)
        c = _ids(ids)
        verdict = np.full(len(self._solids), _OUT, dtype=np.int64)
        c = c[self._area2[c] > 0.0]                        # a zero-area sliver cannot be crossed inside
        if len(c) == 0:
            return verdict
        e1, e2 = self._e1[c], self._e2[c]
        s = o - self._v0[c]
        h = np.cross(dv, e2)
        det = np.einsum("ij,ij->i", e1, h)
        flat = np.abs(det) <= EDGE_EPS * self._area2[c]
        safe = np.where(flat, 1.0, det)
        u = np.einsum("ij,ij->i", s, h) / safe
        qv = np.cross(s, e1)
        v = (qv @ dv) / safe
        t = np.einsum("ij,ij->i", e2, qv) / safe
        w = 1.0 - u - v
        ahead = (t > 0.0) & ~flat
        strictly = (u > EDGE_EPS) & (v > EDGE_EPS) & (w > EDGE_EPS)
        touching = (u >= -EDGE_EPS) & (v >= -EDGE_EPS) & (w >= -EDGE_EPS)
        if np.any(ahead & touching & ~strictly):
            return None                                    # through an edge or a corner
        if np.any(flat):
            # parallel to a triangle: countable unless the ray runs in that triangle's plane
            off = np.abs(np.einsum("ij,ij->i", s[flat], self._nrm[c[flat]])) / self._area2[c[flat]]
            if np.any(off <= self._tri_margin[c[flat]]):
                return None
        hit = ahead & strictly
        if not np.any(hit):
            return verdict
        own, th = self._owner[c[hit]], t[hit]
        leaving = (self._nrm[c[hit]] @ dv) > 0.0           # crossing from material into air
        counts = np.bincount(own, minlength=len(self._solids))
        solids, at = _first_per_group(own, th)              # each solid's nearest crossing
        for k, lv in zip(solids.tolist(), leaving[at].tolist(), strict=True):
            odd = counts[k] % 2 == 1
            verdict[k] = _IN if (odd and lv) else _OUT if (not odd and not lv) else _UNSURE
        return verdict

    def _mesh_verdicts(self, p) -> np.ndarray | None:
        """Per solid IN / OUT / UNSURE from two countable mesh rays that must agree."""
        first = None
        for d in _DIRECTIONS:
            v = self._mesh_ray(p, d)
            if v is None:
                continue
            if first is None:
                first = v
                continue
            return np.where(first == v, first, _UNSURE)
        return None

    # --------------------------------------------------------------------- exact rays ----
    def _touch_order(self, faces: np.ndarray, o: np.ndarray, d: np.ndarray, beyond: float):
        """The faces among `faces` (global indices) the line o + t d (t >= beyond) can meet,
        nearest first, each with the smallest t at which it could: where the line first enters
        one of the face's balls (a bare face: its exact box)."""
        nf = len(self._faces)
        t_in, t_out = _slab(o, d, self._face_lo[faces], self._face_hi[faces])
        live = faces[(t_in <= t_out) & (t_out >= beyond)]
        touch = np.full(nf, np.inf)
        bare = live[self._bare[live]]
        if len(bare):
            b_in, _b_out = _slab(o, d, self._face_lo[bare], self._face_hi[bare])
            touch[bare] = np.maximum(b_in, beyond)
        starts, ends = self._ball_start[live], self._ball_end[live]
        counts = ends - starts
        total = int(counts.sum())
        sel = np.repeat(starts - np.concatenate(([0], np.cumsum(counts)[:-1])), counts) + np.arange(total)
        if len(sel):
            v = self._ball_c[sel] - o
            t = v @ d
            r = self._ball_r[sel]
            perp2 = np.einsum("ij,ij->i", v, v) - t * t
            ok = perp2 <= r * r
            half = np.sqrt(np.maximum(r * r - perp2, 0.0))
            ok &= (t + half) >= beyond
            if np.any(ok):
                np.minimum.at(touch, self._ball_f[sel[ok]], np.maximum(t[ok] - half[ok], beyond))
        hit = np.flatnonzero(np.isfinite(touch))
        order = np.argsort(touch[hit], kind="stable")
        return hit[order], touch[hit][order]

    def _distance_floor(self, k: int, p, radius: float) -> np.ndarray:
        """Per face (global index), how close to `p` the true face of solid `k` can come: its
        triangles' distance less the face's margin, or a straying rim segment's distance less its
        capsule's reach, whichever is less - for the faces with mesh within `radius`; any other
        face comes no closer than `radius` less its reach. A bare face: -inf (its box decides)."""
        import vtk

        pv = np.asarray(p, dtype=float)
        floor = radius - np.maximum(self._face_margin, self._face_cap_reach)
        floor[self._bare] = -np.inf
        ids = vtk.vtkIdList()
        self._box_locator.FindCellsWithinBounds(_cube(p, radius), ids)
        c = _ids(ids)
        if len(c):
            c = c[self._owner[c] == k]
        if len(c):
            np.minimum.at(floor, self._tri_face[c], _point_triangle_distance(pv, self._tris[c]) - self._tri_margin[c])
        if self._near_caps is not None:
            c = self._near_caps.in_cube(p, radius)
            if len(c):
                c = c[self._face_in_solid[k, self._cap_face[c]]]
            if len(c):
                np.minimum.at(floor, self._cap_face[c],
                              _point_segment_distance(pv, self._caps[c, 0], self._caps[c, 1]) - self._cap_r[c])
        return floor

    def _intersector(self, k: int, face: int):
        """The exact line-face cutter for global face `face` as solid `k` holds it, built once (it
        keeps the face's own approximation for every later ray), as the classifier builds its own."""
        from OCP.BRep import BRep_Tool
        from OCP.BRepGProp import BRepGProp_Face
        from OCP.IntCurvesFace import IntCurvesFace_Intersector

        got = self._cutters.get((k, face))
        if got is None:
            occ = self._occurrence[k][face]
            got = self._cutters[(k, face)] = (IntCurvesFace_Intersector(occ, BRep_Tool.Tolerance_s(occ)),
                                              BRepGProp_Face(occ))
        return got

    def _exact_ray(self, k: int, p, toward: int = -1) -> int:
        """IN / OUT for solid `k` by the classifier's own rule - the side of the true surface the
        nearest crossing of a ray faces - on rays cut exactly, face by face nearest first, stopping
        once no face can be nearer than the nearest crossing. Two readable rays must agree. UNSURE
        when they do not, or when too few rays read cleanly: a nearest crossing on the surface, on
        a face's rim (an edge or a corner), at a graze, or two faces at one point. The first ray
        runs straight at the nearest triangle `toward` when there is one: its nearest crossing is
        that close, so only the faces right around the point are cut."""
        from OCP.gp import gp_Pnt, gp_Vec
        from OCP.IntCurveSurface import IntCurveSurface_Tangent
        from OCP.TopAbs import TopAbs_IN

        idx = self._solid_faces[k]
        if len(idx) == 0:
            return _UNSURE
        o = np.asarray(p, dtype=float)
        eps = 1e-9 * max(self._size, 1e-9) + self._tol
        directions = [np.asarray(d, dtype=float) for d in _DIRECTIONS[:3]]
        radius = 4.0 * self._typical
        if toward >= 0:
            # at the nearest triangle's closest point, nudged a tenth of the way to its centre so
            # the ray lands inside it rather than on its rim
            tri = self._tris[toward]
            aim = 0.9 * _closest_on_triangles(o, tri[None])[0] + 0.1 * tri.mean(axis=0) - o
            length = float(np.linalg.norm(aim))
            if length > eps:
                directions.insert(0, aim / length)
                radius = 3.0 * (length + float(self._tri_margin[toward]))
        # how close each nearby face can come to the point: a face cannot be crossed sooner than
        # that, which orders the faces far more tightly than their balls do when the point sits
        # among many small faces
        floor = self._distance_floor(k, p, radius)
        said: list[int] = []
        for dv in directions:
            faces, touch = self._touch_order(idx, o, dv, -eps)
            if len(faces):
                touch = np.maximum(touch, floor[faces])
                order = np.argsort(touch, kind="stable")
                faces, touch = faces[order], touch[order]
            line = _line(o, dv)
            crossings: list[tuple[float, bool, bool]] = []   # (w, leaving, clean)
            nearest = math.inf
            for f, tt in zip(faces.tolist(), touch.tolist(), strict=True):
                if tt > nearest + eps:
                    break                                  # no face left can be nearer
                cutter, face_props = self._intersector(k, f)
                cutter.Perform(line, -eps, math.inf if not math.isfinite(nearest) else nearest + 2.0 * eps)
                if not cutter.IsDone():
                    crossings.append((max(tt, 0.0), False, False))
                    nearest = min(nearest, max(tt, 0.0))
                    continue
                for i in range(1, cutter.NbPnt() + 1):
                    w = float(cutter.WParameter(i))
                    clean = (w > eps and cutter.State(i) == TopAbs_IN
                             and cutter.Transition(i) != IntCurveSurface_Tangent)
                    leaving = False
                    if clean:
                        pnt, nrm = gp_Pnt(), gp_Vec()
                        face_props.Normal(cutter.UParameter(i), cutter.VParameter(i), pnt, nrm)
                        nv = np.array([nrm.X(), nrm.Y(), nrm.Z()])
                        nn = float(np.linalg.norm(nv))
                        along = float(nv @ dv) / nn if nn > 0 else 0.0
                        clean = abs(along) > GRAZE_SINE
                        leaving = along > 0.0
                    crossings.append((max(w, 0.0), leaving, clean))
                    nearest = min(nearest, max(w, 0.0))
            if crossings:
                crossings.sort()
                first = crossings[0]
                if not first[2] or (len(crossings) > 1 and crossings[1][0] - first[0] <= eps):
                    continue                               # unreadable, or two faces at one point
                said.append(_IN if first[1] else _OUT)
            else:
                said.append(_OUT)                          # nothing of the solid along the ray
            if len(said) == 2:
                # two readable rays must agree: one cutter can miss a root the other ray avoids
                return said[0] if said[0] == said[1] else _UNSURE
        return _UNSURE

    # ------------------------------------------------------------------- the questions ----
    def inside_any(self, p) -> bool:
        try:
            return self._inside_any(p)
        except Exception as exc:  # noqa: BLE001 - never a wrong answer for a fast one: ask exactly
            logger.warning("scout probe: fast answer failed (%s: %s); answering exactly",
                           type(exc).__name__, str(exc)[:200])
            self.n_classifier += 1
            return self.inside_any_exact(p)

    def _inside_any(self, p) -> bool:
        p = (float(p[0]), float(p[1]), float(p[2]))
        pv = np.asarray(p)
        # a solid whose reach misses the point does not hold it
        maybe = [k for k in range(len(self._solids))
                 if np.all(pv >= self._solid_lo[k]) and np.all(pv <= self._solid_hi[k])]
        near = self._near(p) if (maybe and self._locator is not None) else {}
        mesh = None
        if self._locator is not None and any(self._trusted[k] and k not in near for k in maybe):
            mesh = self._mesh_verdicts(p)
        how = 0                                            # 0 mesh, 1 exact ray, 2 classifier
        answer = False
        for k in maybe:
            v = _UNSURE
            if self._trusted[k]:
                if k not in near and mesh is not None:
                    v = int(mesh[k])
                if v == _UNSURE:
                    v = self._exact_ray(k, p, near.get(k, -1))
                    how = max(how, 1)
            if v == _UNSURE:
                v = _IN if self._classify(k, p) else _OUT
                how = 2
            if v == _IN:
                answer = True
                break
        if how == 0:
            self.n_mesh += 1
        elif how == 1:
            self.n_ray += 1
        else:
            self.n_classifier += 1
        return answer

    # -------------------------------------------------------------------------- lines ----
    def _line_cuts(self, origin, direction, beyond: float, first_only: bool) -> float:
        """The smallest parameter past `beyond` at which the line meets a face (inf when none),
        cut exactly face by face nearest first; with `first_only`, any such parameter."""
        o = np.asarray(origin, dtype=float)
        d = np.asarray(direction, dtype=float)
        faces, touch = self._touch_order(np.arange(len(self._faces)), o, d, beyond)
        best = math.inf
        for f, tt in zip(faces.tolist(), touch.tolist(), strict=True):
            if tt > best:
                break                                      # no face left can meet it sooner
            for w in _cut(self._faces[f], o, d):
                if w > beyond and w < best:
                    best = w
                    if first_only:
                        return best
        return best

    def meets_beyond(self, origin, direction, beyond: float) -> bool:
        """The line through `origin` along the unit `direction` meets a face past `beyond`."""
        try:
            return math.isfinite(self._line_cuts(origin, direction, beyond, first_only=True))
        except Exception as exc:  # noqa: BLE001 - never a wrong answer for a fast one: cut exactly
            logger.warning("scout probe: fast line cut failed (%s: %s); cutting exactly", type(exc).__name__, str(exc)[:200])
            return self.meets_beyond_exact(origin, direction, beyond)

    def first_beyond(self, origin, direction, beyond: float) -> float:
        """The first parameter past `beyond` at which the line meets a face, 0.0 when none."""
        try:
            best = self._line_cuts(origin, direction, beyond, first_only=False)
        except Exception as exc:  # noqa: BLE001 - never a wrong answer for a fast one: cut exactly
            logger.warning("scout probe: fast line cut failed (%s: %s); cutting exactly", type(exc).__name__, str(exc)[:200])
            return self.first_beyond_exact(origin, direction, beyond)
        return best if math.isfinite(best) else 0.0


__all__ = ["EDGE_EPS", "GRAZE_SINE", "NEAR_STRAYS", "ExactProbe", "LazyProbe", "PartProbe", "make_probe"]
