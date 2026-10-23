# Responsibility: Read the geometry an FEA plan sizes from off a labelled triangle surface: how thick
# the wall is under every face, the sharp inside corners, and the round features (holes, fillets,
# notches and grooves) with their radii - each a hot spot with a place, a type and a size.
# Boundaries: numbers in, numbers out. It reads no file, stores nothing and asks no model; it plans
# nothing either (the refinement each hot spot gets is FEA build step 4's). cad/fea_measure.py builds
# the surface from a CAD file (one label per CAD face) or a triangle file (one per smooth region).
# Collaborates with: cad/fea_measure.py (the only caller), VTK's cell locator for the rays when it is
# installed (a plain numpy ray test otherwise).
"""FEA HOT SPOTS AND WALL THICKNESS, from triangles.

Everything here works in millimetres on a closed, outward-facing triangle surface whose triangles
each carry a label: the CAD face they belong to, or for a triangle file the smooth region.

* WALL THICKNESS: from points spread over every face, a ray along the inward normal to the first
  surface it meets. The length is the wall under that point (the "ray method" of thickness
  analysis). Each face keeps its thinnest and median reading; the part keeps its thinnest.
* SHARP INSIDE CORNERS: where two faces meet in a valley (re-entrant edge) and turn by at least
  SHARP_TURN_DEG. Their stress never converges under refinement, so each one is CAPPED.
* ROUND FEATURES: concave cylinders and tori, exact from the CAD where the face says so, fitted to
  the triangles otherwise. How far the concave arc goes round decides what it is: all the way round
  is a hole, at least NOTCH_SPAN_DEG is a notch (a groove when it also runs round an axis), less is
  a fillet. Convex rounds raise no stress and are not hot spots.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

#: A valley between two faces is a SHARP INSIDE CORNER when the faces turn by at least this much
#: (an L-bracket's unfilleted inside corner turns 90). Tangent joins (a fillet meeting its faces)
#: turn by almost nothing.
SHARP_TURN_DEG = 30.0
#: Triangles of a triangle file are one smooth region while neighbours turn by less than this.
SMOOTH_TURN_DEG = 25.0
#: Neighbouring triangles this close in direction lie on one flat (a triangle file's planes).
FLAT_TURN_DEG = 0.5
#: Two curved triangles of a triangle file are one round when their bends are this alike (the
#: larger curvature over the smaller): a shaft's 2 mm shoulder fillet and the 12 mm shaft it runs
#: into are tangent, but bend six times apart.
CURVATURE_RATIO = 1.6
#: A concave arc that goes this far round is a whole HOLE.
HOLE_SPAN_DEG = 355.0
#: A concave arc of at least this much (and less than a hole) is a NOTCH or GROOVE; less is a FILLET.
#: A fillet blends two faces (90 degrees for a square corner); a notch's round bottom is a half circle.
NOTCH_SPAN_DEG = 150.0
#: A fit counts when the triangles sit within this fraction of its radius (root mean square).
FIT_TOLERANCE = 0.03
#: Wall-thickness rays across the whole part, and at least this many on every face.
THICKNESS_RAYS = 6000
MIN_FACE_RAYS = 8
#: The most hot spots the record keeps (sharpest and smallest first); the count says if more. The
#: plan refines only the ones kept, so this is a guard against a runaway count, set well above any
#: real part: a 6-body F1 car has 208 (200 dropped 4 holes and 4 fillets from its plan, 21 Oct).
MAX_HOT_SPOTS = 2000

#: hot-spot types
HOLE, FILLET, NOTCH, SHARP_CORNER = "hole", "fillet", "notch", "sharp_corner"


@dataclass
class Surface:
    """A closed triangle surface in millimetres, outward facing, every triangle labelled."""
    xyz: np.ndarray          # (n, 3) float, mm
    tris: np.ndarray         # (m, 3) int
    label: np.ndarray        # (m,) int - index into names
    names: list[str]         # Face_1..Face_N (a CAD file) or Region_1..Region_N (a triangle file)
    #: Which solid each label belongs to (a CAD file of several bodies), or None when unknown (a
    #: triangle file: a sealed cavity's wall is a separate piece of the same body there).
    body: np.ndarray | None = None
    #: Each label's own model tolerance in mm (the CAD face's), or None when unknown.
    tol_mm: np.ndarray | None = None

    def __post_init__(self) -> None:
        a, b, c = (self.xyz[self.tris[:, k]] for k in range(3))
        cross = np.cross(b - a, c - a)
        twice = np.linalg.norm(cross, axis=1)
        self.areas = 0.5 * twice
        self.normals = cross / np.maximum(twice, 1e-300)[:, None]
        self.centroids = (a + b + c) / 3.0
        lo, hi = self.xyz.min(axis=0), self.xyz.max(axis=0)
        self.diag = float(np.linalg.norm(hi - lo)) or 1.0


@dataclass
class EdgePairs:
    """Every edge two triangles share: its two vertices, the two triangles, how far their normals
    turn across it (degrees) and whether it is a valley (concave)."""
    verts: np.ndarray        # (k, 2)
    tri_a: np.ndarray        # (k,)
    tri_b: np.ndarray        # (k,)
    turn_deg: np.ndarray     # (k,)
    concave: np.ndarray      # (k,) bool


def edge_pairs(s: Surface) -> EdgePairs:
    """The shared edges of the surface (an edge with one triangle, or more than two, is left out)."""
    m = len(s.tris)
    half = np.vstack([s.tris[:, [0, 1]], s.tris[:, [1, 2]], s.tris[:, [2, 0]]])
    owner = np.tile(np.arange(m), 3)
    key = np.sort(half, axis=1)
    order = np.lexsort((key[:, 1], key[:, 0]))
    key, owner = key[order], owner[order]
    same = np.all(key[1:] == key[:-1], axis=1)
    # runs of exactly two: an edge whose next is the same and whose previous and after-next are not
    starts = np.flatnonzero(same)
    prev_same = np.concatenate([[False], same])[starts]
    next_same = np.concatenate([same, [False]])[starts + 1]
    pick = starts[~prev_same & ~next_same]
    ta, tb = owner[pick], owner[pick + 1]
    na, nb = s.normals[ta], s.normals[tb]
    turn = np.degrees(np.arccos(np.clip(np.einsum("ij,ij->i", na, nb), -1.0, 1.0)))
    mid = 0.5 * (s.xyz[key[pick, 0]] + s.xyz[key[pick, 1]])
    # A VALLEY: each triangle's far side lies on the outward side of the other's plane
    lean = np.einsum("ij,ij->i", na, s.centroids[tb] - mid) + np.einsum("ij,ij->i", nb, s.centroids[ta] - mid)
    return EdgePairs(verts=key[pick], tri_a=ta, tri_b=tb, turn_deg=turn, concave=lean > 0)


# ------------------------------------------------------------------------- wall thickness ----
class _Rays:
    """First hit of a ray on the surface: VTK's static cell locator when VTK is installed, else a
    plain numpy test against every triangle (fine for the small parts the tests use)."""

    def __init__(self, s: Surface):
        self.s = s
        self.loc = None
        try:
            from vtkmodules.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray
            from vtkmodules.vtkCommonCore import vtkPoints
            from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData, vtkStaticCellLocator

            pts = vtkPoints()
            pts.SetData(numpy_to_vtk(np.ascontiguousarray(s.xyz, dtype=float), deep=True))
            cells = vtkCellArray()
            offsets = np.arange(0, 3 * len(s.tris) + 1, 3, dtype=np.int64)
            cells.SetData(numpy_to_vtkIdTypeArray(offsets, deep=True),
                          numpy_to_vtkIdTypeArray(np.ascontiguousarray(s.tris.ravel(), dtype=np.int64), deep=True))
            pd = vtkPolyData()
            pd.SetPoints(pts)
            pd.SetPolys(cells)
            loc = vtkStaticCellLocator()
            loc.SetDataSet(pd)
            loc.BuildLocator()
            self.pd, self.loc = pd, loc
        except ImportError:
            self.loc = None

    def hits(self, origins: np.ndarray, dirs: np.ndarray, reach: float) -> tuple[np.ndarray, np.ndarray]:
        """Distance along each unit direction to the first triangle met, and which triangle
        (nan and -1 where none within reach). Nothing is judged here: see wall_reach."""
        if self.loc is None:
            return _numpy_hits(self.s, origins, dirs, reach)
        from vtkmodules.vtkCommonCore import reference

        out = np.full(len(origins), np.nan)
        cell = np.full(len(origins), -1, dtype=np.int64)
        t, sub, cid = reference(0.0), reference(0), reference(0)
        x, pc = [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]
        for i, (o, d) in enumerate(zip(origins, dirs, strict=True)):
            end = o + d * reach
            if self.loc.IntersectWithLine(o.tolist(), end.tolist(), 0.0, t, x, pc, sub, cid):  # type: ignore[call-overload]
                out[i] = float(t) * reach  # type: ignore[arg-type]
                cell[i] = int(cid)
        return out, cell


#: A ray that meets a triangle at more than about 80 degrees from its normal is GRAZING it: a ray
#: from a point beside an edge runs along the neighbouring face and "hits" it at once. That is not
#: a wall (a 100 mm ring read a 0.26 mm wall on its outer face that way), so it reads nothing; the
#: opposite side of a real wall is met nearly head on.
GRAZE_COS = 0.17
#: THE FLOOR UNDER A READING, as a fraction of the part's diagonal: a surface met closer than this
#: (or than twice the CAD faces' own tolerance, if that is more) is the place the ray started from -
#: a coincident face, two bodies touching, the tessellation's own wobble - and the ray goes on
#: through it. A 620 mm car read a 0.00064 mm "wall" in a pinned hole that way (20 Oct).
FLOOR_FRACTION = 1e-5
#: A ray goes on through at most this many surfaces that are not its wall (another body's face, or
#: one under the floor) before it gives up and reads nothing.
MAX_HOPS = 4
#: A ray that ends on a face meeting its own at an ACUTE CONVEX EDGE (the material between them
#: narrower than TAPER_DEG) is reading a TAPER: the wedge's thickness runs to nothing at that edge
#: however fine the mesh, so it is a sharp edge, not a wall. A flat strip beside a round read 0.08
#: to 0.44 mm that way on the F1 car, and a V-slot extrusion's 45 degree lip 0.05 mm where its
#: median is 1.2 mm. Across a rod whose round face is split in two halves the halves meet smoothly,
#: and that reading still counts.
TAPER_DEG = 88.0
#: A FACE'S WALL needs this many of its rays to agree: its wall is the thinnest reading that at
#: least this many readings (itself included) sit within AGREE_RATIO of, so one stray ray cannot set
#: a face's wall, nor the part's. A face fewer of its rays read (a sliver, a strip its rays only
#: graze) has no wall of its own: the faces round it cover the material under it.
AGREEING_RAYS = 3
AGREE_RATIO = 1.25
#: A WALL THAT IS THIN ONLY IN PLACES (PLAN-fea-3, 22 Oct): a face whose thinnest agreed reading
#: is under VARY_SHARE of its median reading is thin only where those readings are - a counterbore
#: that runs within 0.05 mm of a bore, on a face whose wall is 3 mm elsewhere (fea0221 asked for
#: 0.025 mm elements over the whole 660 mm2 face: 298M elements). Its readings under VARY_SHARE of
#: the median are THIN SPOTS, grouped where they lie within THIN_LINK sample spacings of each other,
#: and the rest of the face keeps the wall its other readings agree on.
VARY_SHARE = 0.5
THIN_LINK = 2.0
#: at most this many thin spots on one face, and on the whole part (the thinnest kept)
MAX_THIN_SPOTS = 24
MAX_THIN_SPOTS_PART = 400


def _numpy_hits(s: Surface, origins: np.ndarray, dirs: np.ndarray, reach: float) -> tuple[np.ndarray, np.ndarray]:
    a = s.xyz[s.tris[:, 0]]
    e1 = s.xyz[s.tris[:, 1]] - a
    e2 = s.xyz[s.tris[:, 2]] - a
    out = np.full(len(origins), np.nan)
    cell = np.full(len(origins), -1, dtype=np.int64)
    for i in range(0, len(origins), 64):
        o, d = origins[i:i + 64, None, :], dirs[i:i + 64, None, :]
        p = np.cross(d, e2[None])
        det = np.einsum("rtk,tk->rt", p, e1)
        ok = np.abs(det) > 1e-14
        inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
        tv = o - a[None]
        u = np.einsum("rtk,rtk->rt", tv, p) * inv
        q = np.cross(tv, e1[None])
        v = np.einsum("rtk,rtk->rt", d, q) * inv
        t = np.einsum("tk,rtk->rt", e2, q) * inv
        hit = ok & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > 0) & (t <= reach)
        t = np.where(hit, t, np.inf)
        best = t.min(axis=1)
        out[i:i + 64] = np.where(np.isfinite(best), best, np.nan)
        cell[i:i + 64] = np.where(np.isfinite(best), t.argmin(axis=1), -1)
    return out, cell


def wall_reach(s: Surface, origins: np.ndarray, dirs: np.ndarray, labels: np.ndarray, *, eps: float) -> np.ndarray:
    """How far each ray (from `eps` inside a face, along its inward normal) runs through the
    material before it LEAVES it - the wall under its start - or nan where that cannot be read.

    A surface met on the way is the far side of the wall only when it is a real exit:
    * it belongs to the body the ray started in: another body's face is two parts touching, not a
      wall, and the ray goes on through it;
    * it lies past the floor (FLOOR_FRACTION of the part, or twice either face's own tolerance): a
      surface closer than that is where the ray started, and the ray goes on through it;
    * the ray meets it head on enough (GRAZE_COS) and from inside the material, its outward normal
      along the ray. A ray that grazes a face, or meets one from outside (it slipped out of the
      solid through a crack in the tessellation), reads nothing;
    * it is not a TAPER: a face meeting the ray's own at an acute convex edge (TAPER_DEG)."""
    rays = _Rays(s)
    n = len(origins)
    m = len(s.names)
    pairs = edge_pairs(s)
    la, lb = s.label[pairs.tri_a], s.label[pairs.tri_b]
    # an acute convex edge: the faces' normals turn by more than 180 - TAPER_DEG across it
    acute = (la != lb) & ~pairs.concave & (pairs.turn_deg > 180.0 - TAPER_DEG)
    wedges = np.unique(np.concatenate([la[acute] * m + lb[acute], lb[acute] * m + la[acute]]))
    floor0 = FLOOR_FRACTION * s.diag
    no_tol = np.zeros(len(s.names))
    tol = s.tol_mm if s.tol_mm is not None else no_tol
    travelled = np.zeros(n)
    out = np.full(n, np.nan)
    live = np.arange(n)
    pos = origins.copy()
    reach = 1.01 * s.diag
    for _hop in range(MAX_HOPS + 1):
        if not len(live):
            break
        dist, cell = rays.hits(pos[live], dirs[live], reach)
        found = cell >= 0
        live, dist, cell = live[found], dist[found], cell[found]
        total = travelled[live] + dist + eps
        hit = s.label[cell]
        floor = np.maximum(floor0, 2.0 * np.maximum(tol[labels[live]], tol[hit]))
        foreign = (s.body[hit] != s.body[labels[live]]) if s.body is not None else np.zeros(len(live), dtype=bool)
        through = foreign | (total < floor)
        cos = np.einsum("ij,ij->i", dirs[live], s.normals[cell])
        taper = np.isin(labels[live] * m + hit, wedges)
        good = ~through & (cos >= GRAZE_COS) & ~taper
        out[live[good]] = total[good]
        # on through the face that is not this ray's wall, from just past it
        live = live[through]
        travelled[live] = total[through]
        pos[live] = origins[live] + dirs[live] * travelled[live, None]
    return out


def agreed_wall(readings) -> int | None:
    """Which of a face's readings (sorted, thinnest first) is its wall: the first that at least
    AGREEING_RAYS readings, itself included, sit within AGREE_RATIO of; None when none is."""
    d = np.asarray(readings, dtype=float)
    if len(d) < AGREEING_RAYS:
        return None
    backed = np.searchsorted(d, AGREE_RATIO * d, side="right") - np.arange(len(d)) >= AGREEING_RAYS
    return int(np.argmax(backed)) if backed.any() else None


def wall_thickness(s: Surface, *, rays: int = THICKNESS_RAYS, seed: int = 0) -> dict:
    """The wall under every face: points spread over each face by area (a fixed seed, so a part
    reads the same twice), each with a ray along the inward normal until it leaves the material
    (wall_reach). A face's wall is the thinnest reading AGREEING_RAYS of its rays agree on.
    {"per_label": {label: (wall, median, point of the wall)}, "min": (value, label, point), "rays"}"""
    rng = np.random.default_rng(seed)
    total = float(s.areas.sum()) or 1.0
    eps = 1e-6 * s.diag
    origins, dirs, labels, points = [], [], [], []
    for lab in np.unique(s.label):
        idx = np.flatnonzero((s.label == lab) & (s.areas > 0))
        if not len(idx):
            continue
        area = float(s.areas[idx].sum())
        n = max(MIN_FACE_RAYS, int(round(rays * area / total)))
        pick = idx[rng.choice(len(idx), size=n, p=s.areas[idx] / area)]
        r1, r2 = rng.random(n), rng.random(n)
        root = np.sqrt(r1)
        a, b, c = (s.xyz[s.tris[pick, k]] for k in range(3))
        p = (1 - root)[:, None] * a + (root * (1 - r2))[:, None] * b + (root * r2)[:, None] * c
        inward = -s.normals[pick]
        points.append(p)
        origins.append(p + inward * eps)
        dirs.append(inward)
        labels.append(np.full(n, lab))
    if not origins:
        return {"per_label": {}, "min": None, "rays": 0}
    P, O, D, L = (np.concatenate(x) for x in (points, origins, dirs, labels))
    dist = wall_reach(s, O, D, L, eps=eps)
    per: dict[int, tuple[float, float, np.ndarray]] = {}
    spots: dict[int, dict] = {}
    best = None
    for lab in np.unique(L):
        k = np.flatnonzero((L == lab) & np.isfinite(dist))
        order = k[np.argsort(dist[k], kind="stable")]
        at = agreed_wall(dist[order])
        if at is None:
            continue        # too few rays read a wall here, or they scatter: no wall of its own
        j = order[at]
        median = float(np.median(dist[k]))
        per[int(lab)] = (float(dist[j]), median, P[j])
        if best is None or dist[j] < best[0]:
            best = (float(dist[j]), int(lab), P[j])
        if dist[j] < VARY_SHARE * median:
            area = float(s.areas[s.label == lab].sum())
            found = thin_spots(P[k], dist[k], median, spacing=math.sqrt(area / max(len(k), 1)))
            if found is not None:
                spots[int(lab)] = found
    return {"per_label": per, "min": best, "rays": int(len(O)), "thin": spots}


def thin_spots(points: np.ndarray, readings: np.ndarray, median: float, *, spacing: float) -> dict | None:
    """A face thin only in places: {"wall": the wall the rest of the face agrees on, "spots": [{"at",
    "radius", "thickness"}]} - the readings under VARY_SHARE of the median grouped into spots, each
    with the centre and the radius that covers its readings (plus one sample spacing, the most the
    thin region can run past the last reading that saw it), and the thinnest reading in it. None
    when the rest of the face reads no agreed wall (then the whole face keeps its thinnest)."""
    thin = readings < VARY_SHARE * median
    rest = np.sort(readings[~thin])
    at = agreed_wall(rest)
    if at is None or not thin.any():
        return None
    pts, vals = points[thin], readings[thin]
    # single-link groups: readings within THIN_LINK sample spacings of each other
    parent = list(range(len(pts)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    link = THIN_LINK * spacing
    for a in range(len(pts)):
        near = np.flatnonzero(np.linalg.norm(pts[a + 1:] - pts[a], axis=1) <= link) + a + 1
        for b in near:
            ra, rb = root(a), root(int(b))
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
    groups: dict[int, list[int]] = {}
    for i in range(len(pts)):
        groups.setdefault(root(i), []).append(i)
    out = []
    for members in groups.values():
        p, v = pts[members], vals[members]
        centre = p.mean(axis=0)
        radius = float(np.max(np.linalg.norm(p - centre, axis=1))) + spacing
        out.append({"at": centre, "radius": radius, "thickness": float(v.min())})
    out.sort(key=lambda r: (r["thickness"], tuple(r["at"])))
    return {"wall": float(rest[at]), "spots": out[:MAX_THIN_SPOTS]}


# ---------------------------------------------------------------------- sharp inside corners ----
def sharp_corners(s: Surface, pairs: EdgePairs, *, turn_deg: float = SHARP_TURN_DEG) -> list[dict]:
    """Every run of valley edges between the same two faces that turn by at least `turn_deg`:
    the faces, the air-side angle (90 for a square inside corner, smaller is sharper), the length
    and a point at its middle."""
    # read off the edges themselves, not the face labels: a triangle file's smooth region can run
    # round a part-filleted edge, so both sides of its sharp stretch carry the same label
    sel = np.flatnonzero(pairs.concave & (pairs.turn_deg >= turn_deg))
    if not len(sel):
        return []
    la, lb = s.label[pairs.tri_a[sel]], s.label[pairs.tri_b[sel]]
    lo, hi = np.minimum(la, lb), np.maximum(la, lb)
    # runs: edges of one face pair that share a vertex
    parent = list(range(len(sel)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    seen: dict[tuple[int, int, int], int] = {}
    for i, e in enumerate(sel):
        for v in pairs.verts[e]:
            key = (int(lo[i]), int(hi[i]), int(v))
            if key in seen:
                parent[root(i)] = root(seen[key])
            else:
                seen[key] = i
    runs: dict[int, list[int]] = {}
    for i in range(len(sel)):
        runs.setdefault(root(i), []).append(i)
    out = []
    for members in runs.values():
        e = sel[members]
        a, b = s.xyz[pairs.verts[e, 0]], s.xyz[pairs.verts[e, 1]]
        length = np.linalg.norm(b - a, axis=1)
        total = float(length.sum())
        if total <= 0:
            continue
        mids = 0.5 * (a + b)
        centre = (mids * length[:, None]).sum(axis=0) / total
        at = mids[int(np.argmin(np.linalg.norm(mids - centre, axis=1)))]
        turn = float((pairs.turn_deg[e] * length).sum() / total)
        out.append({"labels": (int(lo[members[0]]), int(hi[members[0]])), "turn_deg": turn,
                    "angle_deg": 180.0 - turn, "length_mm": total, "at_mm": at})
    out.sort(key=lambda c: (c["angle_deg"], -c["length_mm"]))
    return out


# --------------------------------------------------------------------------- round features ----
@dataclass
class Arc:
    """A round surface: a cylinder (radius about its axis) or a torus (radius of its tube, about a
    circle round its axis). `span_deg` is how far the curved arc goes round; `around_deg` how far a
    torus runs round its axis. Concave: the material is on the far side from the centre."""
    kind: str                         # "cylinder" | "torus"
    radius: float                     # mm
    span_deg: float
    concave: bool
    axis: np.ndarray
    centre: np.ndarray                # a point on the axis (cylinder: the middle of its length)
    labels: set[int] = field(default_factory=set)
    length: float = 0.0               # a cylinder's extent along its axis, mm
    around_deg: float = 0.0           # a torus's run round its axis
    major: float = 0.0                # a torus's distance from axis to tube centre, mm
    basis: str = "exact"              # "exact" (the CAD's own surface) | "fit" (fitted to triangles)
    at: np.ndarray | None = None      # a point on the surface, for the pin
    points: np.ndarray | None = None  # points on the surface: where a cylinder's arc actually runs


def _covered_deg(angles: np.ndarray) -> float:
    """How far round a set of directions goes: 360 less the widest empty gap between them."""
    if len(angles) < 2:
        return 0.0
    a = np.sort(np.mod(angles, 2 * math.pi))
    gaps = np.diff(np.concatenate([a, [a[0] + 2 * math.pi]]))
    widest = float(gaps.max())
    # A RING DRAWN IN FACETS is closed: its widest gap is just one more facet (a coarse STL's
    # 20-sided hole leaves 18 degrees between its sides, a notch's open side 180)
    steps = gaps[gaps > 1e-6]
    if widest <= math.radians(30.0) and widest <= 2.5 * float(np.median(steps)):
        return 360.0
    return float(np.degrees(2 * math.pi - widest))


def _frame(axis: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    helper = np.array([1.0, 0.0, 0.0]) if abs(axis[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    e1 = np.cross(axis, helper)
    e1 /= np.linalg.norm(e1)
    return e1, np.cross(axis, e1)


def _circle(x: np.ndarray, y: np.ndarray, w: np.ndarray) -> tuple[float, float, float, float] | None:
    """Weighted least-squares circle (Kasa): centre, radius and the rms distance off it."""
    A = np.column_stack([2 * x, 2 * y, np.ones_like(x)]) * np.sqrt(w)[:, None]
    rhs = (x * x + y * y) * np.sqrt(w)
    try:
        (cx, cy, c), *_ = np.linalg.lstsq(A, rhs, rcond=None)
    except np.linalg.LinAlgError:
        return None
    r2 = c + cx * cx + cy * cy
    if not np.isfinite(r2) or r2 <= 0:
        return None
    r = math.sqrt(r2)
    off = np.hypot(x - cx, y - cy) - r
    return float(cx), float(cy), r, float(np.sqrt(np.average(off * off, weights=w)))


def _fit_cylinder(p: np.ndarray, n: np.ndarray, w: np.ndarray) -> Arc | None:
    m = (n * w[:, None]).T @ n / w.sum()
    vals, vecs = np.linalg.eigh(m)
    if vals[0] > 0.01 or vals[1] < 1e-4:          # normals not all square to one axis / all one way
        return None
    axis = vecs[:, 0]
    e1, e2 = _frame(axis)
    x, y = p @ e1, p @ e2
    fit = _circle(x, y, w)
    if fit is None:
        return None
    cx, cy, r, rms = fit
    if rms > FIT_TOLERANCE * r:
        return None
    nx, ny = n @ e1, n @ e2
    concave = float(np.sum(w * (nx * (x - cx) + ny * (y - cy)))) < 0
    z = p @ axis
    centre = cx * e1 + cy * e2 + axis * 0.5 * (z.min() + z.max())
    return Arc(kind="cylinder", radius=r, span_deg=_covered_deg(np.arctan2(ny, nx)), concave=concave,
               axis=axis, centre=centre, length=float(z.max() - z.min()), basis="fit")


def _fit_revolution(p: np.ndarray, n: np.ndarray, w: np.ndarray, diag: float) -> Arc | None:
    """A surface of revolution: every normal line meets the axis (a linear fit in line
    coordinates), then the profile in the axis's half-plane is fitted with a circle (the tube)."""
    c0 = np.average(p, axis=0, weights=w)
    scale = float(np.sqrt(np.average(np.sum((p - c0) ** 2, axis=1), weights=w))) or 1.0
    q = (p - c0) / scale
    rows = np.hstack([np.cross(q, n), n]) * np.sqrt(w)[:, None]
    _u, sv, vt = np.linalg.svd(rows, full_matrices=False)
    if sv[-2] <= 0 or sv[-1] / sv[-2] > 0.2:      # no single axis (a sphere, a plane, noise)
        return None
    a, mom = vt[-1, :3], vt[-1, 3:]
    na = float(np.linalg.norm(a))
    if na < 1e-6:
        return None
    axis = a / na
    on_axis = c0 + scale * np.cross(a, mom) / (na * na)
    d = p - on_axis
    z = d @ axis
    radial = d - z[:, None] * axis
    rho = np.linalg.norm(radial, axis=1)
    if np.any(rho <= 0):
        return None
    ru = radial / rho[:, None]
    fit = _circle(rho, z, w)
    if fit is None:
        return None
    r0, z0, r, rms = fit
    if rms > FIT_TOLERANCE * r or r > 0.5 * diag:
        return None
    n_rho, n_z = np.einsum("ij,ij->i", n, ru), n @ axis
    concave = float(np.sum(w * (n_rho * (rho - r0) + n_z * (z - z0)))) < 0
    e1, e2 = _frame(axis)
    return Arc(kind="torus", radius=r, span_deg=_covered_deg(np.arctan2(n_z, n_rho)), concave=concave,
               axis=axis, centre=on_axis + axis * z0, major=r0,
               around_deg=_covered_deg(np.arctan2(d @ e2, d @ e1)), basis="fit")


def face_samples(s: Surface, tri_idx: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """One CAD face's tessellation as fit samples: its vertices (which lie ON the surface, where a
    triangle's centre sits inside a curve by the chord's sag), each with the area-weighted normal
    of its triangles in the face."""
    t = s.tris[tri_idx]
    verts, inv = np.unique(t.ravel(), return_inverse=True)
    nsum = np.zeros((len(verts), 3))
    w = np.zeros(len(verts))
    a = np.repeat(s.areas[tri_idx], 3)
    np.add.at(nsum, inv, np.repeat(s.normals[tri_idx], 3, axis=0) * a[:, None])
    np.add.at(w, inv, a / 3.0)
    return s.xyz[verts], _unit_rows(nsum), w


def patch_samples(s: Surface, pairs: EdgePairs, edges: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """A triangle file's curved stretch as fit samples: the ends of the edges where the surface
    bends (curved_patches), each with the mean normal across its bends. A flat's big border
    triangle, taken into the stretch along with a fillet, adds its edge on the fillet and nothing
    from far across the flat."""
    v = pairs.verts[edges]
    n = _unit_rows(s.normals[pairs.tri_a[edges]] + s.normals[pairs.tri_b[edges]])
    length = np.linalg.norm(s.xyz[v[:, 1]] - s.xyz[v[:, 0]], axis=1)
    verts, inv = np.unique(v.ravel(), return_inverse=True)
    nsum = np.zeros((len(verts), 3))
    w = np.zeros(len(verts))
    np.add.at(nsum, inv, np.repeat(n * length[:, None], 2, axis=0))
    np.add.at(w, inv, np.repeat(length / 2.0, 2))
    return s.xyz[verts], _unit_rows(nsum), w


def _unit_rows(v: np.ndarray) -> np.ndarray:
    return v / np.maximum(np.linalg.norm(v, axis=1), 1e-300)[:, None]


def fit_round(p: np.ndarray, n: np.ndarray, w: np.ndarray, diag: float) -> Arc | None:
    """A cylinder or a torus fitted to surface samples (points, unit normals, weights), or None when
    they are neither (flat, free-form, too few). Fitted twice: samples far off the first fit (where
    a stretch runs on into a neighbouring face) are set aside for the second."""
    if len(p) < 6 or w.sum() <= 0:
        return None
    mean = np.average(n, axis=0, weights=w)
    if np.linalg.norm(mean) > 1e-9:
        spread = np.degrees(np.arccos(np.clip(n @ (mean / np.linalg.norm(mean)), -1.0, 1.0)))
        if float(spread.max()) < 2.0:
            return None                            # flat
    keep = np.ones(len(p), dtype=bool)
    arc = None
    for _ in range(2):
        arc = _fit_cylinder(p[keep], n[keep], w[keep]) or _fit_revolution(p[keep], n[keep], w[keep], diag)
        if arc is None:
            return None
        off = _off(arc, p)
        cut = max(3.0 * float(np.median(off)), 0.02 * arc.radius)
        nxt = off <= cut
        if nxt.sum() < 6 or np.array_equal(nxt, keep):
            break
        keep = nxt
    assert arc is not None
    arc.at = _nearest(p[keep], w[keep])
    arc.points = p[keep]
    return arc


def _off(arc: Arc, p: np.ndarray) -> np.ndarray:
    d = p - arc.centre
    z = d @ arc.axis
    rho = np.linalg.norm(d - z[:, None] * arc.axis, axis=1)
    if arc.kind == "cylinder":
        return np.abs(rho - arc.radius)
    return np.abs(np.hypot(rho - arc.major, z) - arc.radius)


def _nearest(c: np.ndarray, w: np.ndarray) -> np.ndarray:
    mid = np.average(c, axis=0, weights=w) if w.sum() > 0 else c.mean(axis=0)
    return c[int(np.argmin(np.linalg.norm(c - mid, axis=1)))]


def merge_coaxial(arcs: list[Arc]) -> list[Arc]:
    """Cylinders of one radius on one axis are one feature: a hole a CAD system drew as two half
    cylinders goes all the way round. The merged arc is the angle their points actually cover
    round the axis, so a fillet split lengthwise into two pieces stays a 90 degree fillet."""
    out: list[Arc] = []
    for arc in sorted(arcs, key=lambda a: (a.kind, a.radius)):
        twin = None
        if arc.kind == "cylinder":
            for o in out:
                if o.kind != "cylinder" or abs(o.radius - arc.radius) > 0.01 * arc.radius:
                    continue
                if abs(float(o.axis @ arc.axis)) < 0.999 or o.concave != arc.concave:
                    continue
                d = arc.centre - o.centre
                if np.linalg.norm(d - (d @ o.axis) * o.axis) > 0.01 * arc.radius + 1e-9:
                    continue
                if abs(float(d @ o.axis)) > 0.5 * (o.length + arc.length) + 0.01 * arc.radius:
                    continue
                twin = o
                break
        if twin is None:
            out.append(arc)
            continue
        z = [s * 0.5 * twin.length for s in (-1, 1)]
        z += [float((arc.centre - twin.centre) @ twin.axis) + s * 0.5 * arc.length for s in (-1, 1)]
        lo, hi = min(z), max(z)
        twin.centre = twin.centre + twin.axis * 0.5 * (lo + hi)
        twin.length = float(hi - lo)
        if twin.points is not None and arc.points is not None:
            pts = np.vstack([twin.points, arc.points])
            e1, e2 = _frame(twin.axis)
            rel = pts - twin.centre
            twin.points = pts
            twin.span_deg = max(twin.span_deg, arc.span_deg, _covered_deg(np.arctan2(rel @ e2, rel @ e1)))
        else:                         # no points to read the cover from: the arcs side by side
            twin.span_deg = min(360.0, twin.span_deg + arc.span_deg)
        twin.labels |= arc.labels
        if arc.basis == "fit":
            twin.basis = "fit"
    return out


def kind_of(arc: Arc) -> tuple[str, str] | None:
    """(hot-spot type, shape word) for a concave round, None for a convex one."""
    if not arc.concave:
        return None
    if arc.kind == "cylinder" and arc.span_deg >= HOLE_SPAN_DEG:
        return HOLE, "hole"
    if arc.span_deg >= NOTCH_SPAN_DEG:
        groove = arc.kind == "torus" and arc.around_deg >= HOLE_SPAN_DEG
        return NOTCH, "groove" if groove else "notch"
    return FILLET, "fillet"


# ----------------------------------------------------------------- triangle-file regions ----
def smooth_regions(s_tris: np.ndarray, pairs: EdgePairs, m: int) -> np.ndarray:
    """A triangle file's faces: triangles joined across every edge that turns by less than
    SMOOTH_TURN_DEG. Region numbers follow the triangles' own order, so a file reads the same twice."""
    join = pairs.turn_deg < SMOOTH_TURN_DEG
    return components(m, pairs.tri_a[join], pairs.tri_b[join])


def curved_patches(s: Surface, pairs: EdgePairs) -> list[tuple[np.ndarray, np.ndarray]]:
    """A triangle file's curved stretches, as (their triangles, their bending edges): triangles
    joined across edges that turn a little (more than a flat, less than a corner), and across the
    flat diagonals of those triangles - but never across a plane's own inside, so a fillet does
    not flood into the faces it blends, nor between triangles that bend unalike
    (CURVATURE_RATIO), so it does not run on into a tangent shaft either."""
    m = len(s.tris)
    curved = (pairs.turn_deg >= FLAT_TURN_DEG) & (pairs.turn_deg < SMOOTH_TURN_DEG)
    # how sharply each triangle bends: the turn across its bending edges over the distance between
    # the two triangles' centres, square to the edge
    ia, ib = pairs.tri_a[curved], pairs.tri_b[curved]
    e = s.xyz[pairs.verts[curved, 1]] - s.xyz[pairs.verts[curved, 0]]
    e = e / np.maximum(np.linalg.norm(e, axis=1), 1e-300)[:, None]
    gap = s.centroids[ib] - s.centroids[ia]
    across = np.linalg.norm(gap - np.einsum("ij,ij->i", gap, e)[:, None] * e, axis=1)
    bend = np.radians(pairs.turn_deg[curved]) / np.maximum(across, 1e-300)
    kappa = np.zeros(m)
    np.maximum.at(kappa, ia, bend)
    np.maximum.at(kappa, ib, bend)
    touching = kappa > 0
    ka, kb = kappa[pairs.tri_a], kappa[pairs.tri_b]
    alike = touching[pairs.tri_a] & touching[pairs.tri_b] & \
        (np.maximum(ka, kb) <= CURVATURE_RATIO * np.minimum(ka, kb))
    flat_inside = pairs.turn_deg < FLAT_TURN_DEG
    join = (curved | flat_inside) & alike
    comp = components(m, pairs.tri_a[join], pairs.tri_b[join])
    tris = np.flatnonzero(touching)
    bends = np.flatnonzero(curved & alike)
    t_groups = _groups(comp[tris], tris)
    e_groups = _groups(comp[pairs.tri_a[bends]], bends)
    return [(t_groups[c], e_groups[c]) for c in sorted(e_groups) if len(e_groups[c]) >= 3]


def _groups(keys: np.ndarray, values: np.ndarray) -> dict[int, np.ndarray]:
    order = np.argsort(keys, kind="stable")
    k, v = keys[order], values[order]
    cuts = np.flatnonzero(np.diff(k)) + 1
    return {int(chunk_k[0]): chunk_v for chunk_k, chunk_v in zip(np.split(k, cuts), np.split(v, cuts), strict=True)
            if len(chunk_k)}


def components(m: int, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    g = coo_matrix((np.ones(len(a)), (a, b)), shape=(m, m))
    _n, comp = connected_components(g, directed=False)
    # renumber by first appearance, so the numbering follows the triangles' order
    _u, first = np.unique(comp, return_index=True)
    rank = np.empty(len(first), dtype=np.int64)
    rank[np.argsort(first)] = np.arange(len(first))
    return rank[comp]


def nesting_depth(xyz: np.ndarray, tris: np.ndarray, comp: np.ndarray) -> np.ndarray:
    """For each piece (comp: a piece number per triangle), how many other pieces enclose it: 0 for
    an outer skin, 1 for a sealed cavity's wall inside it, 2 for an island in that cavity. Read by
    the parity of a ray from one of its points through each piece whose box holds it."""
    n = int(comp.max()) + 1 if len(comp) else 0
    depth = np.zeros(n, dtype=np.int64)
    if n < 2:
        return depth
    pts = xyz[tris]                                          # (m, 3, 3)
    lo = np.full((n, 3), np.inf)
    hi = np.full((n, 3), -np.inf)
    np.minimum.at(lo, comp, pts.min(axis=1))
    np.maximum.at(hi, comp, pts.max(axis=1))
    first = np.zeros(n, dtype=np.int64)
    first[comp[::-1]] = np.arange(len(comp))[::-1]
    d = np.array([0.3119, 0.5741, 0.7570])
    d /= np.linalg.norm(d)
    for i in range(n):
        o = xyz[tris[first[i], 0]]
        for j in range(n):
            if j == i or np.any(lo[i] < lo[j]) or np.any(hi[i] > hi[j]):
                continue
            tj = pts[comp == j]
            a, e1, e2 = tj[:, 0], tj[:, 1] - tj[:, 0], tj[:, 2] - tj[:, 0]
            p = np.cross(d, e2)
            det = np.einsum("ij,ij->i", p, e1)
            ok = np.abs(det) > 1e-14
            inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
            tv = o - a
            u = np.einsum("ij,ij->i", tv, p) * inv
            q = np.cross(tv, e1)
            v = (q @ d) * inv
            t = np.einsum("ij,ij->i", e2, q) * inv
            if int(np.count_nonzero(ok & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > 0))) % 2 == 1:
                depth[i] += 1
    return depth


def bodies_of(tris: np.ndarray, pairs: EdgePairs) -> int:
    """How many separate pieces the triangles make (joined across shared edges)."""
    if not len(tris):
        return 0
    return int(components(len(tris), pairs.tri_a, pairs.tri_b).max() + 1)


__all__ = ["FILLET", "HOLE", "HOLE_SPAN_DEG", "MAX_HOT_SPOTS", "NOTCH", "NOTCH_SPAN_DEG", "SHARP_CORNER",
           "SHARP_TURN_DEG", "SMOOTH_TURN_DEG", "THICKNESS_RAYS", "VARY_SHARE", "thin_spots", "Arc", "EdgePairs", "Surface",
           "agreed_wall", "bodies_of", "components", "wall_reach", "curved_patches", "edge_pairs", "face_samples", "fit_round", "kind_of", "merge_coaxial", "nesting_depth", "patch_samples",
           "sharp_corners", "smooth_regions", "wall_thickness"]
