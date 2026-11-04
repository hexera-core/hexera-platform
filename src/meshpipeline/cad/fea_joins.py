# Responsibility: Measure where the parts of an FEA assembly meet on their SURFACES, pair by pair - faces that touch, faces that face each other across a small gap (the small-gap join, PLAN-fea-18), faces that cross, a part wholly inside another - and check what the general fuse said against it (the join check).
# Boundaries: geometry only, deterministic, no model call, no persistence. It never asks a solid whether a point is inside it except for a part with no surface near the other (wholly inside): a body whose surface crosses itself answers that question wrongly, and the surfaces do not. A face another part passes through that also crosses or lies on another face of its own body (a zero-thickness skin) never joins the two. It decides nothing about meshing; cad/fea_assembly.py uses what it measures.
# Collaborates with: cad/fea_assembly.py (record() and stage() call measure() and check(); the interfaces it returns are in fea_assembly's record shape), engines/fea_assembly_deck.py (writes a gap joint's tolerance), application/fea_check.py (says the gap joins on the stage).
"""WHERE THE PARTS MEET, MEASURED ON THEIR SURFACES (PLAN-fea-18).

The general fuse (cad/fea_assembly) decides what touches by cutting every part against every other,
and it trusts each part's inside: a part whose surface crosses itself (a tube whose hole cut left a
skin of no thickness over the hole) makes the fuse put a bracket "100% inside" a tube it does not even
reach, and miss the same joint on the mirrored side. The surfaces themselves are read here instead:

* TOUCHING: faces of two parts whose outward normals face each other (within 45 degrees of opposite)
  and lie within the fuse's own fuzzy distance, over at least MIN_JOIN_AREA_SHARE of the smaller
  part's size squared.
* LINE: surfaces that come within the gap limit only along a line, an edge or at a point (no faces
  facing each other over the least joint area: a tube on a plate, a plate's edge on a frame) - joined
  there with the gap limit as the solver's reach.
* GAP (the small-gap join): the same, across a gap of up to gap_limit_mm(): 0.5 mm, or 2% of the
  smaller part's box diagonal when that is less - the bonded-contact tolerance a solver user sets
  for welds and threads the CAD does not draw (a boss sleeved into a clearance hole, a bolt in its
  tapped bore). A bolt in a clearance hole a millimetre wider stays a clearance.
* CROSSING: the surfaces of two parts pass through each other (interference), found by the section
  of their faces running through the inside of a face, not along its edge.
* INSIDE: a part with no surface near the other, lying wholly inside it.

A face another part passes through, and that crosses or lies on another face of its own part, is a
SKIN (part of a self-crossing surface, or a doubled face of no thickness): its crossing never joins
parts, and the faces are said (the sizing reads them too). A face that only faces the other part, at
a touch or across a gap, is evidence whatever its own part does elsewhere.

check() compares the fuse's interfaces with these: a pair the fuse joins whose surfaces are apart, a
part "wholly inside" another whose box it is not inside, or a touch the fuse missed - each a finding.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any

import numpy as np

#: THE SMALL-GAP JOIN (PLAN-fea-18): faces of two parts facing each other across at most this gap are
#: joined, never more than GAP_JOIN_SHARE of the smaller part's box diagonal (a 5 mm pin: 0.1 mm).
GAP_JOIN_MM = 0.5
GAP_JOIN_SHARE = 0.02
#: A gap measured AT the limit is within it, in any unit: the comparison carries this relative slack.
#: Without it the same stud punch read as "5 pairs across a 0.5 mm gap" on the check (millimetres) and
#: "4 meet only along a line, 1 across a gap" on the run (the part staged in metres, where 0.5 mm came
#: out a hair over 0.0005 m) - two answers for one part (demo dry run, 2 Nov).
LIMIT_RTOL = 1e-6


def within(gap, limit: float):
    """Whether a measured gap (a number or an array) lies within `limit`, the limit itself included in
    any unit (LIMIT_RTOL)."""
    return gap <= limit * (1.0 + LIMIT_RTOL)
#: Two faces FACE each other when their outward normals are within this angle of opposite.
FACING_DEG = 45.0
#: The least facing area that makes a joint: this share of the smaller part's box diagonal, squared
#: ((1% of the part)^2: 0.17 mm^2 on an 18 mm boss).
MIN_JOIN_AREA_SHARE = 1e-4
#: At most this many pairs of faces that come near each other are measured in one assembly.
MAX_FACE_PAIRS = 200000
#: The measurement stops (and says it did not finish) past this many seconds.
BUDGET_S = 600.0

TOUCHING, GAP, OVERLAP = "touching", "gap", "overlap"


def gap_limit_mm(diag_a_mm: float, diag_b_mm: float) -> float:
    """The small-gap join's limit for two parts (millimetres): 0.5 mm, or 2% of the smaller part's
    box diagonal when that is less."""
    return float(min(GAP_JOIN_MM, GAP_JOIN_SHARE * min(float(diag_a_mm), float(diag_b_mm))))


@dataclass
class Joint:
    """Two parts' meeting, measured: its kind, each part's faces (1-based, the part's own order), the
    facing area and the gap (file units), where it is."""
    a: int
    b: int
    kind: str
    faces_a: list[int] = field(default_factory=list)
    faces_b: list[int] = field(default_factory=list)
    area: float = 0.0
    gap: float | None = None
    gap_max: float | None = None
    at: list[float] | None = None
    limit: float = 0.0
    #: met along a line, an edge or at a point: no faces facing each other over the least joint area
    line: bool = False


@dataclass
class Measured:
    """Every pair's joint, the skins found (part -> its faces that cross their own part), whether
    the measurement finished, and its seconds."""
    joints: dict[tuple[int, int], Joint] = field(default_factory=dict)
    skins: dict[int, list[int]] = field(default_factory=dict)
    #: pairs whose surfaces come within the gap limit but make no joint: whether they meet only on skins
    near: dict[tuple[int, int], dict] = field(default_factory=dict)
    #: pairs OpenCASCADE could not measure (the reason): the fuse's answer stands for them
    unmeasured: dict[tuple[int, int], str] = field(default_factory=dict)
    complete: bool = True
    seconds: float = 0.0
    tol: float = 0.0


# #
# shapes
# #

def _faces(shape) -> list:
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, m)
    return [TopoDS.Face_s(m.FindKey(i)) for i in range(1, m.Extent() + 1)]


def _edges(face) -> list:
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(face, TopAbs_EDGE, m)
    return [TopoDS.Edge_s(m.FindKey(i)) for i in range(1, m.Extent() + 1)]


def _box(shape) -> tuple[np.ndarray, np.ndarray]:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    b = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, b, False, False)
    x0, y0, z0, x1, y1, z1 = b.Get()
    return np.array([x0, y0, z0]), np.array([x1, y1, z1])


def _meet(a, b, pad: float) -> bool:
    return not (np.any(a[1] + pad < b[0]) or np.any(b[1] + pad < a[0]))


def _inside(inner, outer, pad: float) -> bool:
    return bool(np.all(inner[0] >= outer[0] - pad) and np.all(inner[1] <= outer[1] + pad))


def _shells(solid):
    """The solid's boundary as a compound of its shells: distances between shells never ask the
    solid what is inside it."""
    from OCP.BRep import BRep_Builder
    from OCP.TopAbs import TopAbs_SHELL
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS_Compound
    from OCP.TopTools import TopTools_IndexedMapOfShape

    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(solid, TopAbs_SHELL, m)
    c = TopoDS_Compound()
    bb = BRep_Builder()
    bb.MakeCompound(c)
    for i in range(1, m.Extent() + 1):
        bb.Add(c, m.FindKey(i))
    return c


def _dist(a, b) -> float:
    """The shapes' distance, or nan when OpenCASCADE cannot say (BRepExtrema can throw StdFail_NotDone,
    or finish without an answer): an unknown distance is never taken as apart, nor as touching."""
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape

    try:
        d = BRepExtrema_DistShapeShape(a, b)
        d.Perform()
        return float(d.Value()) if d.IsDone() else math.nan
    except Exception:  # noqa: BLE001 - OpenCASCADE's failures arrive as plain exceptions; unknown
        return math.nan


def _dist_at(a, b) -> tuple[float, np.ndarray]:
    """The shapes' distance and the middle of their closest points; (nan, origin) when OpenCASCADE
    cannot say."""
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape

    try:
        d = BRepExtrema_DistShapeShape(a, b)
        d.Perform()
        if not d.IsDone() or d.NbSolution() == 0:
            return math.nan, np.zeros(3)
        p, q = d.PointOnShape1(1), d.PointOnShape2(1)
        return float(d.Value()), np.array([(p.X() + q.X()) / 2, (p.Y() + q.Y()) / 2, (p.Z() + q.Z()) / 2])
    except Exception:  # noqa: BLE001 - unknown, never a crash
        return math.nan, np.zeros(3)


def _area(shape) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    g = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, g)
    return abs(float(g.Mass()))


def _volume(shape) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    g = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, g)
    return abs(float(g.Mass()))


# #
# a part's surface, sampled
# #

#: Each part is tessellated once (on a copy: the part's own shape is never touched) to this share of
#: its box diagonal, and its triangles split until no side is longer than SAMPLE_SHARE of it: the
#: points that find which faces of two parts come near each other at all.
CHORD_SHARE = 0.002
SAMPLE_SHARE = 0.01
#: Where two faces come near, their triangles there are split again, down to half the least joint's
#: side (never more than half the gap limit), and every sample is put exactly on its face.
MAX_SAMPLES = 60000
#: ...and no finer than this many samples over the region where two faces come near.
SAMPLES_PER_PAIR = 600


@dataclass
class _Surface:
    """A part's surface as points: each a triangle's centre, with the face it is on, its area and the
    triangle's outward unit normal; and the triangles themselves, by face."""
    centres: np.ndarray
    areas: np.ndarray
    normals: np.ndarray
    face: np.ndarray
    tris: np.ndarray            # (T, 3, 3), the split triangles the centres are of
    tree: Any = None
    #: each face's own tessellation in its (u, v) parameters, and a tree of its triangles' centres:
    #: where a point projected onto the face's surface lies inside the trimmed face
    uv: dict = field(default_factory=dict)
    by_face: dict = field(default_factory=dict)


def _area_of(t: np.ndarray) -> float:
    return float(0.5 * np.linalg.norm(np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0]), axis=1).sum())


def _cells(tris: np.ndarray, size: float) -> list[np.ndarray]:
    """The triangles grouped by the cube of side `size` their centre lies in (each group's first is its
    sample), in a fixed order."""
    key = np.floor(tris.mean(axis=1) / max(size, 1e-300)).astype(np.int64)
    _u, inv = np.unique(key, axis=0, return_inverse=True)
    inv = np.asarray(inv).ravel()
    order = np.argsort(inv, kind="stable")
    bounds = np.flatnonzero(np.diff(inv[order])) + 1
    return np.split(order, bounds)


def _split(tris: np.ndarray, longest: float, cap: int) -> np.ndarray:
    """Triangles split in four until none has a side longer than `longest` (or there are `cap`)."""
    out = [tris]
    while True:
        t = out[-1]
        if not len(t):
            break
        side = np.max(np.linalg.norm(t[:, [1, 2, 0]] - t, axis=2), axis=1)
        big = side > longest
        if not big.any() or sum(len(x) for x in out) + 3 * int(big.sum()) > cap:
            break
        keep, b = t[~big], t[big]
        m01, m12, m20 = (b[:, 0] + b[:, 1]) / 2, (b[:, 1] + b[:, 2]) / 2, (b[:, 2] + b[:, 0]) / 2
        out[-1] = keep
        out.append(np.concatenate([np.stack([b[:, 0], m01, m20], 1), np.stack([m01, b[:, 1], m12], 1),
                                   np.stack([m20, m12, b[:, 2]], 1), np.stack([m01, m12, m20], 1)]))
    return np.concatenate(out) if len(out) > 1 else out[0]


def _surface(solid, faces: list, diag: float) -> _Surface:
    """The part's surface points (see _Surface), from a tessellation of a copy of it."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
    from OCP.TopAbs import TopAbs_REVERSED
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS
    from scipy.spatial import cKDTree

    from meshpipeline.cad.occ_box import mesh_to_size

    copy = BRepBuilderAPI_Copy(solid, True, False).Shape()
    # through the guarded helper (cad/occ_box): the part's own size sets the chord, never a stray unit
    mesh_to_size(copy, CHORD_SHARE, 0.3)
    cfaces = _faces(copy)
    longest = max(SAMPLE_SHARE * diag, 1e-12)
    blocks, fids = [], []
    uvs: dict[int, tuple[np.ndarray, Any]] = {}
    for k, f in enumerate(cfaces):
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(TopoDS.Face_s(f), loc)
        if tri is None or tri.NbTriangles() == 0:
            continue
        trsf = loc.Transformation()
        pts = np.array([[q.X(), q.Y(), q.Z()] for q in (tri.Node(n).Transformed(trsf)
                                                        for n in range(1, tri.NbNodes() + 1))])
        t = np.array([[tri.Triangle(n).Value(i) for i in (1, 2, 3)] for n in range(1, tri.NbTriangles() + 1)]) - 1
        if tri.HasUVNodes():
            # THE FACE'S CLEAR INSIDE: its triangles none of whose corners lies on one of its edges - a
            # point inside one of those is inside the trimmed face, with no classifier to ask
            rim: set[int] = set()
            for e in _edges(f):
                poly = BRep_Tool.PolygonOnTriangulation_s(e, tri, loc)
                if poly is not None:
                    rim.update(int(poly.Node(n)) - 1 for n in range(1, poly.NbNodes() + 1))
            inner = ~np.isin(t, np.fromiter(rim, dtype=np.int64, count=len(rim))).any(axis=1)
            if inner.any():
                uvn = np.array([[q.X(), q.Y()] for q in (tri.UVNode(n) for n in range(1, tri.NbNodes() + 1))])
                ut = uvn[t[inner]]
                uvs[k] = (ut, cKDTree(ut.mean(axis=1)))
        if f.Orientation() == TopAbs_REVERSED:
            t = t[:, [0, 2, 1]]
        tp = _split(pts[t], longest, 200000)
        blocks.append(tp)
        fids.append(np.full(len(tp), k, dtype=np.int64))
    if not blocks:
        return _Surface(np.zeros((0, 3)), np.zeros(0), np.zeros((0, 3)), np.zeros(0, dtype=np.int64), np.zeros((0, 3, 3)))
    tp = np.concatenate(blocks)
    cr = np.cross(tp[:, 1] - tp[:, 0], tp[:, 2] - tp[:, 0])
    area = 0.5 * np.linalg.norm(cr, axis=1)
    ok = area > 0
    tp, cr, area, fid = tp[ok], cr[ok], area[ok], np.concatenate(fids)[ok]
    centres = tp.mean(axis=1)
    return _Surface(centres=centres, areas=area, normals=cr / (2.0 * area[:, None]), face=fid, tris=tp,
                    tree=cKDTree(centres), uv=uvs)


def _inside_uv(uv: np.ndarray, mesh) -> np.ndarray:
    """Whether each (u, v) lies inside the face, read off the face's own tessellation in its
    parameters (the trimmed face, as its triangles cover it)."""
    tris, tree = mesh
    k = min(8, len(tris))
    _d, idx = tree.query(uv, k=k)
    idx = np.asarray(idx).reshape(len(uv), k)
    inside = np.zeros(len(uv), dtype=bool)
    for j in range(k):
        t = tris[idx[:, j]]
        a, b, c = t[:, 0], t[:, 1], t[:, 2]
        v0, v1, v2 = c - a, b - a, uv - a
        d00, d01, d11 = (v0 * v0).sum(1), (v0 * v1).sum(1), (v1 * v1).sum(1)
        d02, d12 = (v0 * v2).sum(1), (v1 * v2).sum(1)
        den = d00 * d11 - d01 * d01
        ok = np.abs(den) > 1e-300
        den = np.where(ok, den, 1.0)
        bu = (d11 * d02 - d01 * d12) / den
        bv = (d00 * d12 - d01 * d02) / den
        inside |= ok & (bu >= 0.0) & (bv >= 0.0) & (bu + bv <= 1.0)
    return inside


def _face_points(surf: _Surface, face: int):
    """One face's surface points and their tree, built once a part."""
    from scipy.spatial import cKDTree

    if face not in surf.by_face:
        pts = surf.centres[surf.face == face]
        surf.by_face[face] = (pts, cKDTree(pts) if len(pts) else None)
    return surf.by_face[face]


#: The per-face tools of the measurement under way (measure() clears it at its start and its end).
_FACE_TOOLS: dict[int, tuple] = {}


def _on_face(points: np.ndarray, face, uv_mesh=None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Each point put exactly on `face` (projected onto its surface): (ok, the point, the face's
    outward unit normal there); not ok where the projection misses the trimmed face (read off the
    face's tessellation in its parameters when given - OpenCASCADE's own classifier takes a quarter of
    a millisecond a point on a face of many holes - else by that classifier)."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepGProp import BRepGProp_Face
    from OCP.BRepTools import BRepTools
    from OCP.BRepTopAdaptor import BRepTopAdaptor_FClass2d
    from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
    from OCP.gp import gp_Pnt, gp_Pnt2d, gp_Vec
    from OCP.TopAbs import TopAbs_IN, TopAbs_ON

    # each face's projector, classifier and normal reader, built once a measurement (a face of many
    # holes takes OpenCASCADE a while to set up for classifying)
    key = face.__hash__()
    if key not in _FACE_TOOLS or not _FACE_TOOLS[key][0].IsSame(face):
        surf = BRep_Tool.Surface_s(face)
        u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
        proj = GeomAPI_ProjectPointOnSurf()
        proj.Init(surf, u0, u1, v0, v1, 1e-9)
        _FACE_TOOLS[key] = (face, proj, BRepTopAdaptor_FClass2d(face, 1e-9), BRepGProp_Face(face))
    _f, proj, cls, props = _FACE_TOOLS[key]
    ok = np.zeros(len(points), dtype=bool)
    foot = np.zeros((len(points), 3))
    nrm = np.zeros((len(points), 3))
    p, n = gp_Pnt(), gp_Vec()
    uv = np.full((len(points), 2), np.nan)
    for k, (x, y, z) in enumerate(points.tolist()):
        try:
            proj.Perform(gp_Pnt(x, y, z))
        except Exception:  # noqa: BLE001, S112 - a point OpenCASCADE cannot project is not on the face
            continue
        if proj.IsDone() and proj.NbPoints() > 0:
            uv[k] = proj.LowerDistanceParameters()
    found = np.isfinite(uv[:, 0])
    # inside the trimmed face: clearly inside its tessellation's inner triangles, else OpenCASCADE's
    # classifier (the same answer, only sooner where the point is well inside)
    clear = np.zeros(len(points), dtype=bool)
    if uv_mesh is not None and found.any():
        clear[found] = _inside_uv(uv[found], uv_mesh)
    inside = clear.copy()
    for k in np.flatnonzero(found & ~clear).tolist():
        try:
            inside[k] = cls.Perform(gp_Pnt2d(float(uv[k, 0]), float(uv[k, 1]))) in (TopAbs_IN, TopAbs_ON)
        except Exception:  # noqa: BLE001 - a point OpenCASCADE cannot classify is not on the face
            inside[k] = False
    for k in np.flatnonzero(inside).tolist():
        u, v = float(uv[k, 0]), float(uv[k, 1])
        try:
            props.Normal(u, v, p, n)
        except Exception:  # noqa: BLE001, S112 - no normal there: the point is not on the face
            continue
        mag = n.Magnitude()
        if mag <= 0:
            continue
        ok[k] = True
        foot[k] = (p.X(), p.Y(), p.Z())
        nrm[k] = (n.X() / mag, n.Y() / mag, n.Z() / mag)
    return ok, foot, nrm


def _facing(sa: _Surface, a: int, fa, sb: _Surface, b: int, fb, limit: float, reach: float, tol: float,
            spacing: float) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """Face a's samples that face face b within `limit`: (their places, their areas, their gaps). Face
    a's triangles within `reach` of face b's points are split to `spacing`, each sample is put exactly
    on face a and measured exactly to face b: it faces b when that is within the limit, their outward
    normals are within FACING_DEG of opposite, and - across a gap - b lies in front of it, not behind."""

    mine = np.flatnonzero(sa.face == a)
    theirs, their_tree = _face_points(sb, b)
    empty = (np.zeros((0, 3)), np.zeros(0), np.zeros(0), 0.0)
    if not len(mine) or not len(theirs):
        return empty
    if their_tree is None:
        return empty
    d, _k = their_tree.query(sa.centres[mine], k=1, distance_upper_bound=reach)
    tris = sa.tris[mine[np.isfinite(d)]]
    if not len(tris):
        return empty
    # COARSE THEN FINE, never more than about SAMPLES_PER_PAIR projections a pass: the band near face b
    # read in cells of `step` (one sample a cell, standing for every triangle in it), only the cells
    # within the limit kept, and those read again in cells of the final spacing
    step = max(spacing, limit, math.sqrt(_area_of(tris) / SAMPLES_PER_PAIR))
    cells = _cells(tris, step)
    reps = np.array([c[0] for c in cells])
    pts = tris[reps].mean(axis=1)
    ok_b, on_b, _n = _on_face(pts, fb, sb.uv.get(b))
    kd, _kk = their_tree.query(pts, k=1)
    # where the centre's projection misses face b (beyond its edge), its nearest point of face b
    dist = np.where(ok_b, np.linalg.norm(on_b - pts, axis=1), kd - (reach - limit))
    near_cells = [c for c, dd in zip(cells, dist.tolist(), strict=True) if dd <= limit + 2.0 * step + tol]
    if not near_cells:
        return empty
    tris = tris[np.concatenate(near_cells)]
    spacing = max(spacing, math.sqrt(_area_of(tris) / SAMPLES_PER_PAIR))
    tris = _split(tris, spacing, MAX_SAMPLES)
    cells = _cells(tris, spacing)
    cr = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    tri_area = 0.5 * np.linalg.norm(cr, axis=1)
    reps = np.array([c[0] for c in cells])
    area = np.array([float(tri_area[c].sum()) for c in cells])
    keep = area > 0
    pts, area = tris[reps[keep]].mean(axis=1), area[keep]
    ok_a, on_a, n_a = _on_face(pts, fa, sa.uv.get(a))
    on_a, n_a, area = on_a[ok_a], n_a[ok_a], area[ok_a]
    ok_b, on_b, n_b = _on_face(on_a, fb, sb.uv.get(b))
    gap = np.full(len(on_a), np.inf)
    gap[ok_b] = np.linalg.norm(on_b[ok_b] - on_a[ok_b], axis=1)
    opposed = np.einsum("ij,ij->i", n_a, n_b) <= -math.cos(math.radians(FACING_DEG))
    ahead = np.einsum("ij,ij->i", on_b - on_a, n_a) >= -tol
    m = ok_b & within(gap, limit) & opposed & ((gap <= tol) | ahead)
    # LYING ON IT, THE SAME SIDE OUT: the two parts' material on the same side of one surface - a body drawn
    # twice, or two parts passing into each other flush with a shared face
    aligned = ok_b & (gap <= tol) & (np.einsum("ij,ij->i", n_a, n_b) >= math.cos(math.radians(FACING_DEG)))
    return on_a[m], area[m], gap[m], float(area[aligned].sum())


# #
# faces that cross
# #

def _edge_mid(edge):
    from OCP.BRepAdaptor import BRepAdaptor_Curve

    c = BRepAdaptor_Curve(edge)
    p = c.Value(0.5 * (c.FirstParameter() + c.LastParameter()))
    return p


def _crossing_points(fa, fb, tol: float) -> list[np.ndarray]:
    """Where two faces' section runs through the inside of either face (not along an edge of both):
    the faces cross there. Empty when they only meet along their edges."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Section
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_WIRE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    sec = BRepAlgoAPI_Section(fa, fb, False)
    sec.SetRunParallel(False)
    sec.SetNonDestructive(True)
    sec.Build()
    if not sec.IsDone():
        return []
    em = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(sec.Shape(), TopAbs_EDGE, em)
    if em.Extent() == 0:
        return []

    def rim(face):
        wm = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(face, TopAbs_WIRE, wm)
        from OCP.BRep import BRep_Builder
        from OCP.TopoDS import TopoDS_Compound
        c = TopoDS_Compound()
        bb = BRep_Builder()
        bb.MakeCompound(c)
        for i in range(1, wm.Extent() + 1):
            bb.Add(c, wm.FindKey(i))
        return c

    ra, rb = rim(fa), rim(fb)
    out = []
    for i in range(1, em.Extent() + 1):
        p = _edge_mid(TopoDS.Edge_s(em.FindKey(i)))
        v = BRepBuilderAPI_MakeVertex(p).Vertex()
        if _dist(v, ra) > tol or _dist(v, rb) > tol:
            out.append(np.array([p.X(), p.Y(), p.Z()]))
    return out


def _skin_faces(solid, faces: list, boxes: list, which: set[int], tol: float, min_area: float,
                cache: dict[int, bool], surf: _Surface | None = None, slack: float = 0.0) -> None:
    """Fill `cache` for each face index in `which`: whether it crosses or lies on another face of its
    own part (a skin, never evidence of a joint). Faces next to each other meet along their shared
    edge, which is no crossing."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    from OCP.TopTools import TopTools_ListOfShape

    for i in sorted(which):
        if i in cache:
            continue
        cache[i] = False
        close: set[int] | None = None
        if surf is not None and len(surf.centres):
            # the own faces whose surface points come within a triangle's size of this face's
            mine = surf.centres[surf.face == i]
            if len(mine):
                d, k = surf.tree.query(mine, k=16, distance_upper_bound=slack + tol)
                close = {int(surf.face[x]) for x in np.asarray(k)[np.isfinite(d)].ravel().tolist()}
        for j, g in enumerate(faces):
            if j == i or (close is not None and j not in close) or not _meet(boxes[i], boxes[j], 2 * tol) \
                    or _dist(faces[i], g) > tol:
                continue
            op = BRepAlgoAPI_Common()
            la, lb = TopTools_ListOfShape(), TopTools_ListOfShape()
            la.Append(faces[i])
            lb.Append(g)
            op.SetArguments(la)
            op.SetTools(lb)
            op.SetFuzzyValue(tol)
            op.SetRunParallel(False)
            op.SetNonDestructive(True)
            op.Build()
            doubled = op.IsDone() and _area(op.Shape()) >= min_area
            if doubled or _crossing_points(faces[i], g, tol):
                cache[i] = True
                break


# #
# the measurement
# #

def measure(parts: list, *, tol: float, mm_per_unit: float, budget_s: float = BUDGET_S) -> Measured:
    """Every pair of parts' joint, measured on their surfaces (see the module text). `tol`: the
    distance taken as touching (the fuse's fuzzy value), in the parts' unit; `mm_per_unit` turns the
    gap limit's millimetres into it."""

    t0 = time.time()
    mm = float(mm_per_unit) or 1.0
    tol = max(float(tol), 1e-12)
    out = Measured(tol=tol)
    _FACE_TOOLS.clear()
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory(prefix="fea_joins_") as tmp:
        try:
            # THE PARTS AS THE WORKERS READ THEM: written once to a B-rep file and read back, here too, so
            # every pair is measured on the very same numbers whether one process or several measure it
            brep = Path(tmp) / "parts.brep"
            parts = _round_trip(parts, brep)
            pairs = _near_pairs(parts, tol=tol, mm=mm)
            workers = _workers(len(pairs))
            if workers > 1 and brep.exists():
                got = _spawned(parts, pairs, workers, tol=tol, mm=mm, budget_s=budget_s, t0=t0)
                if got is not None:
                    out.joints, out.near, out.skins, out.complete, out.unmeasured = got
                    out.seconds = round(time.time() - t0, 2)
                    return out
            return _measure(parts, tol=tol, mm=mm, budget_s=budget_s, t0=t0, out=out, only=set(pairs))
        finally:
            _FACE_TOOLS.clear()


def _round_trip(parts: list, brep) -> list:
    """The parts written to `brep` and read back (the same solids, in the same order); the parts as
    given when the file cannot be written or read."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape

    from meshpipeline.cad.fea_assembly import compound, solids

    try:
        if not BRepTools.Write_s(compound(parts), str(brep)):
            return parts
        shape = TopoDS_Shape()
        if not BRepTools.Read_s(shape, str(brep), BRep_Builder()):
            return parts
        back = solids(shape)
    except Exception:  # noqa: BLE001 - measured as given
        return parts
    if len(back) != len(parts):
        brep.unlink(missing_ok=True)
        return parts
    return back


#: THE PAIRS IN PARALLEL: every pair of parts is measured on its own, so the pairs are shared among this
#: many worker processes (the same answers, in the same order, sooner); one a core up to this many, and
#: only when there are at least WORKER_PAIRS pairs a worker (a worker takes a second or two to start).
MAX_WORKERS = 8
WORKER_PAIRS = 4
#: WHAT A WORKER TAKES: about WORKER_BASE_BYTES to start (Python, OpenCASCADE, numpy, scipy), plus
#: WORKER_BREP_FACTOR times the size of the B-rep file of the parts it measures (their shapes, faces,
#: tessellated surfaces and search trees). The workers running at once take at most WORKER_MEMORY_SHARE
#: of the memory the job has free when they start; one always runs.
WORKER_BASE_BYTES = 400 * 2**20
WORKER_BREP_FACTOR = 500.0
WORKER_MEMORY_SHARE = 0.6
#: RUNS A WORKER: the pairs are cut into this many runs of neighbours per worker, handed out as workers
#: come free. One run a worker left one worker with every pair of a heavy part (lab, 28 Oct: a 42-part
#: launch-pad support 161 -> 247 s, a 114-part boiler 68 -> 101 s); three per worker spread them
#: (208 s and 87 s), each run still reading only its own parts.
WORKER_RUNS = 3


def _near_pairs(parts: list, *, tol: float, mm: float) -> list[tuple[int, int]]:
    """The pairs of parts whose boxes come within twice their gap limit: the only ones measured."""
    boxes = [_box(s) for s in parts]
    diags = [float(np.linalg.norm(b[1] - b[0])) for b in boxes]
    out = []
    for i, j in combinations(range(len(parts)), 2):
        limit = max(gap_limit_mm(diags[i] * mm, diags[j] * mm) / mm, tol)
        if _meet(boxes[i], boxes[j], 2 * limit):
            out.append((i, j))
    return out


def _workers(n_pairs: int) -> int:
    """How many runs the pairs are cut into: by cores and pairs (memory decides how many of them run at
    once, see _spawned)."""
    import os

    if n_pairs < 2 * WORKER_PAIRS:
        return 1
    try:
        cores = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cores = os.cpu_count() or 1
    return max(1, min(MAX_WORKERS, cores, n_pairs // WORKER_PAIRS))


def _memory_free() -> float | None:
    """The bytes the job has free (its container's limit less what it uses, else the machine's); None
    when it cannot be read (then cores alone decide)."""
    from meshpipeline.sandbox.liveness import memory_in_use

    got = memory_in_use()
    if got is None:
        return None
    used, limit = got
    return float(max(limit - used, 0))


def _chunks(pairs: list, n: int) -> list[list]:
    """The pairs in `n` runs of neighbours (in pair order, near-equal counts): a part's pairs mostly in
    one run, so each worker reads few parts, never the whole assembly."""
    n = max(1, min(n, len(pairs)))
    size, extra = divmod(len(pairs), n)
    out, k = [], 0
    for c in range(n):
        m = size + (1 if c < extra else 0)
        out.append(pairs[k:k + m])
        k += m
    return [c for c in out if c]


def _worker_need(brep_bytes: int) -> float:
    """The memory a worker reading a B-rep file of `brep_bytes` is counted at."""
    return WORKER_BASE_BYTES + WORKER_BREP_FACTOR * float(brep_bytes)


def _spawned(parts: list, pairs: list, workers: int, *, tol: float, mm: float, budget_s: float, t0: float):
    """The pairs measured in fresh Python processes (`python -m meshpipeline.cad.fea_joins`), merged in
    pair order: (joints, near, skins, complete, unmeasured), or None when a worker could not start, ran
    out of time or failed (then the caller measures every pair here, one by one).

    The pairs are cut into WORKER_RUNS runs of neighbours a worker, at most `workers` running. Each run's worker reads a B-rep file of ITS OWN
    parts only (written here from the parts as measure() read them back: the same numbers), and only as
    many run at once as the job's free memory holds (WORKER_MEMORY_SHARE of it, at _worker_need each; one
    always runs). When they grow past that share anyway, the newest is stopped and run again later, with
    fewer at once (so too a worker the kernel kills for memory beside others). Never `workers` copies of the whole assembly: 27 Oct, 8 workers of 3.5 GB each, every
    one reading all 406 parts of a laser cutter, got a 32 GiB job killed.
    Fresh processes, never forked copies: a fork of a process with threads can stall on a lock another
    thread held (26 Oct, a test run that never ended)."""
    import pickle
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    from OCP.BRepTools import BRepTools

    from meshpipeline.cad.fea_assembly import compound

    left = budget_s - (time.time() - t0)
    if left <= 30.0:
        return None
    # every run answers within the measure's own budget (each is started with what is left of it), so
    # the deadline is that budget and a grace; what finished runs measured is kept when it passes
    deadline = t0 + budget_s + 120.0
    with tempfile.TemporaryDirectory(prefix="fea_joins_") as tmp:
        jobs: list[tuple[list[str], Path, float]] = []
        specs: dict[str, dict] = {}
        running: list[tuple[subprocess.Popen, Path, float, list[str]]] = []
        joints: dict = {}
        near: dict = {}
        unmeasured: dict = {}
        skins: dict[int, set[int]] = {}
        complete = True
        try:
            for k, chunk in enumerate(_chunks(pairs, workers * WORKER_RUNS)):
                index = sorted({p for pair in chunk for p in pair})
                brep = Path(tmp) / f"parts{k}.brep"
                if not BRepTools.Write_s(compound([parts[p] for p in index]), str(brep)):
                    return None
                spec = Path(tmp) / f"job{k}.json"
                specs[str(spec)] = {"brep": str(brep), "index": index, "pairs": [list(x) for x in chunk],
                                    "tol": tol, "mm": mm}
                out = Path(tmp) / f"out{k}.pkl"
                jobs.append(([sys.executable, "-m", "meshpipeline.cad.fea_joins", str(spec), str(out)], out,
                             _worker_need(brep.stat().st_size)))
            free = _memory_free()
            room = math.inf if free is None else WORKER_MEMORY_SHARE * free
            floor = 0.0 if free is None else (1.0 - WORKER_MEMORY_SHARE) * free
            most = min(workers, len(jobs))
            while jobs or running:
                # START what the memory holds, in order; one always runs
                while jobs and (not running or (len(running) < most
                                                and sum(r[2] for r in running) + jobs[0][2] <= room)):
                    # a run started late (memory let only a few run at once) gets what is left of the
                    # budget; with nothing left, the runs not started are not measured (the fuse's answer
                    # stands for their pairs, as when one process runs out of time)
                    left = budget_s - (time.time() - t0)
                    if left <= 5.0:
                        jobs.clear()
                        complete = False
                        break
                    cmd, out, need = jobs.pop(0)
                    Path(cmd[-2]).write_text(json.dumps({**specs[cmd[-2]], "budget_s": left}))
                    running.append((subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                     stderr=subprocess.DEVNULL), out, need, cmd))
                done = [r for r in running if r[0].poll() is not None]
                if not done:
                    if time.time() > deadline:
                        # past the budget: what the finished runs measured is kept, the rest not measured
                        complete = False
                        break
                    # THE GUARD: the workers grew past what was counted for them - the newest one stopped and
                    # put back to run later, and never again this many at once (it never stops the last one)
                    now = _memory_free() if free is not None else None
                    if now is not None and now < floor and len(running) > 1:
                        proc, out, need, cmd = running.pop()
                        proc.kill()
                        proc.wait()
                        jobs.insert(0, (cmd, out, need))
                        most = len(running)
                    time.sleep(0.2)
                    continue
                for r in done:
                    running.remove(r)
                    proc, out, need, cmd = r
                    if proc.returncode < 0 and running:
                        # killed (the kernel's out-of-memory killer) beside others: again later, fewer at once
                        jobs.insert(0, (cmd, out, need))
                        most = max(1, len(running))
                        continue
                    if proc.returncode != 0 or not out.exists():
                        return None
                    j, n, sk, c, um = pickle.loads(out.read_bytes())  # noqa: S301 - our own worker's answer, in our own temp dir
                    joints.update(j)
                    near.update(n)
                    unmeasured.update(um)
                    for part, fs in sk.items():
                        skins.setdefault(part, set()).update(fs)
                    complete = complete and c
        except (OSError, subprocess.SubprocessError, ValueError, EOFError, pickle.UnpicklingError):
            return None
        finally:
            for proc, *_rest in running:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
    return ({k: joints[k] for k in sorted(joints)}, {k: near[k] for k in sorted(near)},
            {k: sorted(v) for k, v in sorted(skins.items())}, complete,
            {k: unmeasured[k] for k in sorted(unmeasured)})


def _worker(spec_path: str, out_path: str) -> int:
    """One worker of _spawned: its own parts read back from its B-rep file, its pairs measured on them,
    the answer written in the assembly's numbering (pairs 0-based, parts 1-based)."""
    import dataclasses
    import pickle

    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape

    from meshpipeline.cad.fea_assembly import solids

    spec = json.loads(open(spec_path).read())
    shape = TopoDS_Shape()
    if not BRepTools.Read_s(shape, spec["brep"], BRep_Builder()):
        return 2
    parts = solids(shape)
    index = [int(p) for p in spec["index"]]
    if len(parts) != len(index):
        return 3
    local = {g: k for k, g in enumerate(index)}
    t0 = time.time()
    res = _measure(parts, tol=float(spec["tol"]), mm=float(spec["mm"]), budget_s=float(spec["budget_s"]), t0=t0,
                   out=Measured(tol=float(spec["tol"])),
                   only={(local[int(i)], local[int(j)]) for i, j in spec["pairs"]})

    def pair(k: tuple[int, int]) -> tuple[int, int]:
        return index[k[0]], index[k[1]]

    joints = {pair(k): dataclasses.replace(v, a=index[v.a - 1] + 1, b=index[v.b - 1] + 1)
              for k, v in res.joints.items()}
    with open(out_path, "wb") as fh:
        pickle.dump((joints, {pair(k): v for k, v in res.near.items()},
                     {index[p - 1] + 1: v for p, v in res.skins.items()}, res.complete,
                     {pair(k): v for k, v in res.unmeasured.items()}), fh)
    return 0


def _measure(parts: list, *, tol: float, mm: float, budget_s: float, t0: float, out: Measured,
             only: set | None = None) -> Measured:
    from OCP.BRep import BRep_Tool
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.TopAbs import TopAbs_IN, TopAbs_VERTEX
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    boxes = [_box(s) for s in parts]
    diags = [float(np.linalg.norm(b[1] - b[0])) for b in boxes]
    faces = [_faces(s) for s in parts]
    fboxes = [[_box(f) for f in fs] for fs in faces]
    surfaces: dict[int, _Surface] = {}
    skin: dict[int, dict[int, bool]] = {}

    def surface(i) -> _Surface:
        if i not in surfaces:
            surfaces[i] = _surface(parts[i], faces[i], diags[i])
        return surfaces[i]

    # A PART LET GO after its last pair: its surface and face tools freed, so what is held is the
    # surfaces of the parts still to be paired, never every part's at once
    todo = [(i, j) for i, j in combinations(range(len(parts)), 2) if only is None or (i, j) in only]
    done_with: dict[int, list[int]] = {}
    for part, n in {p: n for n, pair in enumerate(todo) for p in pair}.items():
        done_with.setdefault(n, []).append(part)

    def let_go(n: int) -> None:
        for part in done_with.get(n, ()):
            surfaces.pop(part, None)
            for f in faces[part]:
                tools = _FACE_TOOLS.get(f.__hash__())
                if tools is not None and tools[0].IsSame(f):
                    del _FACE_TOOLS[f.__hash__()]

    pairs_tried = 0
    for n, (i, j) in enumerate(todo):
        if n:
            let_go(n - 1)
        if time.time() - t0 > budget_s:
            out.complete = False
            break
        limit = max(gap_limit_mm(diags[i] * mm, diags[j] * mm) / mm, tol)
        if not _meet(boxes[i], boxes[j], 2 * limit):
            continue
        try:
            si, sj = surface(i), surface(j)
            reach = limit + SAMPLE_SHARE * (diags[i] + diags[j]) + tol
            if not len(si.centres) or not len(sj.centres):
                continue
            d_ij, k_ij = sj.tree.query(si.centres, k=1, distance_upper_bound=reach)
            d_ji, k_ji = si.tree.query(sj.centres, k=1, distance_upper_bound=reach)
            n_i, n_j = np.isfinite(d_ij), np.isfinite(d_ji)
            if not n_i.any() and not n_j.any():
                # NO SURFACE NEAR: the smaller wholly inside the other, or apart
                small, big = (i, j) if diags[i] <= diags[j] else (j, i)
                if _inside(boxes[small], boxes[big], tol):
                    vm = TopTools_IndexedMapOfShape()
                    TopExp.MapShapes_s(parts[small], TopAbs_VERTEX, vm)
                    if vm.Extent():
                        p = BRep_Tool.Pnt_s(TopoDS.Vertex_s(vm.FindKey(1)))
                        if BRepClass3d_SolidClassifier(parts[big], p, tol).State() == TopAbs_IN:
                            c = 0.5 * (boxes[small][0] + boxes[small][1])
                            out.joints[(i, j)] = Joint(i + 1, j + 1, OVERLAP, area=0.0, at=[float(x) for x in c],
                                                       limit=limit)
                continue
            # THE FACES THAT COME NEAR: each pair of a face of one part and the face of the other nearest it,
            # with the closest their points come (the tessellation's word: within a triangle's size)
            approx: dict[tuple[int, int], float] = {}
            for fa_, fb_, dd in zip(si.face[n_i].tolist(), sj.face[k_ij[n_i]].tolist(), d_ij[n_i].tolist(), strict=True):
                approx[(fa_, fb_)] = min(approx.get((fa_, fb_), math.inf), dd)
            for fa_, fb_, dd in zip(si.face[k_ji[n_j]].tolist(), sj.face[n_j].tolist(), d_ji[n_j].tolist(), strict=True):
                approx[(fa_, fb_)] = min(approx.get((fa_, fb_), math.inf), dd)
            pairs_tried += len(approx)
            if pairs_tried > MAX_FACE_PAIRS:
                out.complete = False
                break
            min_area = MIN_JOIN_AREA_SHARE * min(diags[i], diags[j]) ** 2
            fine = max(min(limit / 2.0, math.sqrt(min_area) / 2.0), tol)
            slack = SAMPLE_SHARE * (diags[i] + diags[j]) + tol
            seen: list[tuple[int, int, np.ndarray, np.ndarray, np.ndarray, float]] = []
            flush: dict[tuple[int, int], float] = {}
            crossing: list[tuple[int, int, list[np.ndarray]]] = []
            for (a, b), dd in sorted(approx.items()):
                pa, aa, ga, on_a = _facing(si, a, faces[i][a], sj, b, faces[j][b], limit, reach, tol, fine)
                _pb, ab, _gb, on_b = _facing(sj, b, faces[j][b], si, a, faces[i][a], limit, reach, tol, fine)
                if min(on_a, on_b) > 0:
                    flush[(a, b)] = min(on_a, on_b)
                if aa.sum() > 0 and ab.sum() > 0:
                    seen.append((a, b, pa, aa, ga, float(ab.sum())))
                    continue
                # surfaces that may cross: only where their points come within a triangle's size
                if dd <= slack and _dist(faces[i][a], faces[j][b]) <= tol:
                    at = _crossing_points(faces[i][a], faces[j][b], tol)
                    if at:
                        crossing.append((a, b, at))
            # SKINS: a face the other part passes through, and that crosses or lies on another face of its
            # own part, is a surface of no thickness there - its crossing joins nothing
            for p, side in ((i, {c[0] for c in crossing}), (j, {c[1] for c in crossing})):
                _skin_faces(parts[p], faces[p], fboxes[p], side, tol, MIN_JOIN_AREA_SHARE * diags[p] ** 2,
                            skin.setdefault(p, {}), surface(p), 2.0 * SAMPLE_SHARE * diags[p])
            sk_i, sk_j = skin.get(i, {}), skin.get(j, {})
            area_a: dict[int, float] = {}
            area_b: dict[int, float] = {}
            touch = 0.0
            gaps: list[tuple[float, float]] = []
            where = np.zeros(3)
            wsum = 0.0
            for a, b, pa, aa, ga, fb_area in seen:
                fa_area = float(aa.sum())
                touch += float(aa[ga <= tol].sum())
                area_a[a] = area_a.get(a, 0.0) + fa_area
                area_b[b] = area_b.get(b, 0.0) + fb_area
                gaps.extend(zip(ga.tolist(), aa.tolist(), strict=True))
                where += (pa * aa[:, None]).sum(axis=0)
                wsum += fa_area
            skinned = any(sk_i.get(c[0]) or sk_j.get(c[1]) for c in crossing)
            crossing = [c for c in crossing if not sk_i.get(c[0]) and not sk_j.get(c[1])]
            total_a, total_b = sum(area_a.values()), sum(area_b.values())
            if min(total_a, total_b) >= min_area:
                g = np.array(gaps)
                order = np.argsort(g[:, 0])
                cum = np.cumsum(g[order, 1])
                gap = 0.0 if touch >= min_area else float(g[order, 0][np.searchsorted(cum, 0.5 * cum[-1])])
                fa_keep = sorted(a + 1 for a, v in area_a.items() if v >= 0.1 * min_area)
                fb_keep = sorted(b + 1 for b, v in area_b.items() if v >= 0.1 * min_area)
                # TOUCHING ALONG A LINE: faces that face each other across a band that closes to nothing (a tube
                # on a plate) touch there, though no touching area is measured - a touch, said as a line one
                line = False
                if gap > tol:
                    a0, b0 = min(((a, b, float(ga.min())) for a, b, _pa, _aa, ga, _fb in seen if len(ga)),
                                 key=lambda r: r[2])[:2]
                    if _dist_at(faces[i][a0], faces[j][b0])[0] <= tol:
                        line = True
                gap_max = float(g[:, 0].max())
                out.joints[(i, j)] = Joint(i + 1, j + 1, TOUCHING if gap <= tol or line else GAP, faces_a=fa_keep,
                                           faces_b=fb_keep, area=min(total_a, total_b),
                                           gap=0.0 if gap <= tol or line else gap,
                                           gap_max=gap_max if gap > tol else 0.0,
                                           at=[float(x) for x in where / max(wsum, 1e-300)], limit=limit, line=line)
            elif crossing or sum(flush.values()) >= min_area:
                # surfaces that cross, or lie on each other the same side out: the parts pass into each other
                if not crossing:
                    crossing = [(a, b, [0.5 * (fboxes[i][a][0] + fboxes[i][a][1])]) for (a, b) in flush]
                pts = np.vstack([np.array(at) for _a, _b, at in crossing])
                out.joints[(i, j)] = Joint(i + 1, j + 1, OVERLAP, faces_a=sorted({a + 1 for a, _b, _at in crossing}),
                                           faces_b=sorted({b + 1 for _a, b, _at in crossing}),
                                           at=[float(x) for x in pts.mean(axis=0)], limit=limit)
            else:
                # NEAR ALONG A LINE, AN EDGE OR A POINT (a tube lying on a plate, a plate's edge on a frame, a
                # ring in a countersink, corners a hair apart): no faces face each other over the least joint
                # area, yet the surfaces come within the gap limit - joined there, as a solver's bonded reach
                # joins whatever lies within it. A skin is no evidence here either.
                close = []
                for (a, b), dd in sorted(approx.items()):
                    if dd > limit + slack or sk_i.get(a) or sk_j.get(b):
                        continue
                    dab, where_ab = _dist_at(faces[i][a], faces[j][b])
                    if within(dab, limit):
                        close.append((a, b, dab, where_ab))
                if close and not skinned:
                    dmin = min(c[2] for c in close)
                    out.joints[(i, j)] = Joint(
                        i + 1, j + 1, TOUCHING if dmin <= tol else GAP, faces_a=sorted({c[0] + 1 for c in close}),
                        faces_b=sorted({c[1] + 1 for c in close}), area=0.0, gap=0.0 if dmin <= tol else dmin,
                        gap_max=limit, at=[float(x) for x in np.mean([c[3] for c in close], axis=0)], limit=limit,
                        line=True)
                else:
                    out.near[(i, j)] = {"skin_only": skinned}
        except Exception as exc:  # noqa: BLE001 - OpenCASCADE threw on this pair (StdFail_NotDone...)
            # ONE PAIR OPENCASCADE CANNOT MEASURE is that pair unmeasured: the fuse's answer stands for it,
            # and the rest of the assembly is measured as ever
            out.joints.pop((i, j), None)
            out.near.pop((i, j), None)
            out.unmeasured[(i, j)] = f"{type(exc).__name__}: {exc}"[:200]
    out.skins = {p + 1: sorted(k + 1 for k, v in c.items() if v) for p, c in skin.items() if any(c.values())}
    out.seconds = round(time.time() - t0, 2)
    return out


def interfaces(measured: Measured, parts: list, volumes: list[float], *, scale: float = 1.0,
               kinds: tuple[str, ...] = (TOUCHING, GAP, OVERLAP)) -> list[dict]:
    """The measured joints of `kinds` in cad/fea_assembly's interface shape (Part_k, Part_k_Face_j,
    millimetres times `scale`), each gap joint with its gap and limit; numbered from Interface_1."""
    from meshpipeline.cad.fea_assembly import face_name, part_id

    out: list[dict[str, Any]] = []
    for (i, j), jt in sorted(measured.joints.items()):
        if jt.kind not in kinds:
            continue
        p, q = i + 1, j + 1
        faces = {part_id(p): [face_name(p, k) for k in jt.faces_a], part_id(q): [face_name(q, k) for k in jt.faces_b]}
        row: dict[str, Any] = {"kind": jt.kind, "parts": [part_id(p), part_id(q)], "faces": faces,
                               "at_mm": [_r(x * scale) for x in (jt.at or [0.0, 0.0, 0.0])],
                               "area_mm2": _r(jt.area * scale * scale) if jt.kind != OVERLAP else None}
        if jt.kind == GAP or jt.line:
            row.update(gap_mm=_r(float(jt.gap or 0.0) * scale), gap_max_mm=_r(float(jt.gap_max or 0.0) * scale),
                       gap_limit_mm=_r(jt.limit * scale))
        if jt.line:
            row["line"] = True
        if jt.kind == OVERLAP:
            inside = not jt.faces_a and not jt.faces_b
            smaller = min(volumes[i], volumes[j]) or 1.0
            row["overlap_share"] = round(min(volumes[i], volumes[j]) / smaller, 6) if inside else None
        out.append(row)
    for k, itf in enumerate(out, start=1):
        itf["id"] = f"Interface_{k}"
    return out


def check(fuse_interfaces: list[dict], parts: list, measured: Measured, *, tol: float,
          mm_per_unit: float = 1.0) -> list[dict]:
    """THE JOIN CHECK: what the fuse said against the surfaces. A finding for every pair the fuse
    joins whose surfaces do not come within the touching distance ("apart"), or meet only on faces
    that cross their own part ("skin_only"); every part the fuse puts wholly inside another whose box
    it is not inside ("not_inside"); and every touch the fuse missed ("missed"). A pair the fuse joins
    over less than the least joint area is no finding. Each finding: {"kind", "parts", "words"}."""
    from meshpipeline.cad.fea_assembly import WHOLLY_INSIDE

    mm = float(mm_per_unit) or 1.0
    boxes = [_box(s) for s in parts]
    said: dict[tuple[int, int], dict] = {}
    for itf in fuse_interfaces:
        p, q = sorted(int(x.split("_")[1]) for x in itf.get("parts") or [])
        said[(p - 1, q - 1)] = itf
    found: list[dict] = []
    for (i, j), itf in sorted(said.items()):
        names = [f"Part_{i + 1}", f"Part_{j + 1}"]
        if (i, j) in measured.unmeasured:
            continue
        if (i, j) not in measured.joints and measured.complete:
            near = measured.near.get((i, j))
            gap = _dist(_shells(parts[i]), _shells(parts[j]))
            if near and near.get("skin_only"):
                found.append({"kind": "skin_only", "parts": names,
                              "words": f"the fuse joins {names[0]} and {names[1]}, but they meet only on faces "
                                       "that cross their own part"})
            elif gap > tol:      # an unknown distance (nan) is no finding
                found.append({"kind": "apart", "parts": names,
                              "words": f"the fuse joins {names[0]} and {names[1]}, but their surfaces are "
                                       f"{gap * mm:.3g} mm apart"})
        if itf.get("kind") == OVERLAP and float(itf.get("overlap_share") or 0.0) >= WHOLLY_INSIDE:
            vi, vj = _volume(parts[i]), _volume(parts[j])
            small, big = (i, j) if vi <= vj else (j, i)
            if not _inside(boxes[small], boxes[big], tol):
                found.append({"kind": "not_inside", "parts": names,
                              "words": f"the fuse puts Part_{small + 1} wholly inside Part_{big + 1}, "
                                       "but it reaches outside that part's box"})
    if measured.complete:
        for (i, j), jt in sorted(measured.joints.items()):
            if jt.kind == TOUCHING and (i, j) not in said:
                found.append({"kind": "missed", "parts": [f"Part_{i + 1}", f"Part_{j + 1}"],
                              "words": f"Part_{i + 1} and Part_{j + 1} touch over {jt.area:.3g} (the file's "
                                       "unit squared), and the fuse did not find it"})
    return found


def _r(v: float) -> float:
    v = float(v)
    return float(f"{v:.6g}") if math.isfinite(v) else v


__all__ = ["BUDGET_S", "FACING_DEG", "GAP", "GAP_JOIN_MM", "GAP_JOIN_SHARE", "Joint", "MIN_JOIN_AREA_SHARE",
           "Measured", "OVERLAP", "TOUCHING", "check", "gap_limit_mm", "interfaces", "measure"]


if __name__ == "__main__":  # a worker of _spawned
    import sys

    # run as the package's own module, so what it pickles is meshpipeline.cad.fea_joins's, not __main__'s
    from meshpipeline.cad import fea_joins as _self

    sys.exit(_self._worker(sys.argv[1], sys.argv[2]))
