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


@dataclass
class Measured:
    """Every pair's joint, the skins found (part -> its faces that cross their own part), whether
    the measurement finished, and its seconds."""
    joints: dict[tuple[int, int], Joint] = field(default_factory=dict)
    skins: dict[int, list[int]] = field(default_factory=dict)
    #: pairs whose surfaces come within the gap limit but make no joint: whether they meet only on skins
    near: dict[tuple[int, int], dict] = field(default_factory=dict)
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
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape

    d = BRepExtrema_DistShapeShape(a, b)
    d.Perform()
    return float(d.Value()) if d.IsDone() else math.inf


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
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopAbs import TopAbs_REVERSED
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS
    from scipy.spatial import cKDTree

    copy = BRepBuilderAPI_Copy(solid, True, False).Shape()
    BRepMesh_IncrementalMesh(copy, max(CHORD_SHARE * diag, 1e-12), False, 0.3, False)
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
            uvn = np.array([[q.X(), q.Y()] for q in (tri.UVNode(n) for n in range(1, tri.NbNodes() + 1))])
            ut = uvn[t]
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
        eps = 1e-6
        inside |= ok & (bu >= -eps) & (bv >= -eps) & (bu + bv <= 1 + eps)
    return inside


def _face_points(surf: _Surface, face: int):
    """One face's surface points and their tree, built once a part."""
    from scipy.spatial import cKDTree

    if face not in surf.by_face:
        pts = surf.centres[surf.face == face]
        surf.by_face[face] = (pts, cKDTree(pts) if len(pts) else None)
    return surf.by_face[face]


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

    surf = BRep_Tool.Surface_s(face)
    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    cls = BRepTopAdaptor_FClass2d(face, 1e-9)
    props = BRepGProp_Face(face)
    ok = np.zeros(len(points), dtype=bool)
    foot = np.zeros((len(points), 3))
    nrm = np.zeros((len(points), 3))
    proj = GeomAPI_ProjectPointOnSurf()
    proj.Init(surf, u0, u1, v0, v1, 1e-9)
    p, n = gp_Pnt(), gp_Vec()
    uv = np.full((len(points), 2), np.nan)
    for k, (x, y, z) in enumerate(points.tolist()):
        proj.Perform(gp_Pnt(x, y, z))
        if proj.IsDone() and proj.NbPoints() > 0:
            uv[k] = proj.LowerDistanceParameters()
    found = np.isfinite(uv[:, 0])
    if uv_mesh is not None and found.any():
        inside = np.zeros(len(points), dtype=bool)
        inside[found] = _inside_uv(uv[found], uv_mesh)
    else:
        inside = np.array([bool(found[k]) and cls.Perform(gp_Pnt2d(float(uv[k, 0]), float(uv[k, 1])))
                           in (TopAbs_IN, TopAbs_ON) for k in range(len(points))], dtype=bool)
    for k in np.flatnonzero(inside).tolist():
        u, v = float(uv[k, 0]), float(uv[k, 1])
        props.Normal(u, v, p, n)
        mag = n.Magnitude()
        if mag <= 0:
            continue
        ok[k] = True
        foot[k] = (p.X(), p.Y(), p.Z())
        nrm[k] = (n.X() / mag, n.Y() / mag, n.Z() / mag)
    return ok, foot, nrm


def _facing(sa: _Surface, a: int, fa, sb: _Surface, b: int, fb, limit: float, reach: float, tol: float,
            spacing: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Face a's samples that face face b within `limit`: (their places, their areas, their gaps). Face
    a's triangles within `reach` of face b's points are split to `spacing`, each sample is put exactly
    on face a and measured exactly to face b: it faces b when that is within the limit, their outward
    normals are within FACING_DEG of opposite, and - across a gap - b lies in front of it, not behind."""

    mine = np.flatnonzero(sa.face == a)
    theirs, their_tree = _face_points(sb, b)
    empty = (np.zeros((0, 3)), np.zeros(0), np.zeros(0))
    if not len(mine) or not len(theirs):
        return empty
    if their_tree is None:
        return (np.zeros((0, 3)), np.zeros(0), np.zeros(0))
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
    ok_b, on_b, _n = _on_face(pts, fb)
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
    ok_a, on_a, n_a = _on_face(pts, fa)
    on_a, n_a, area = on_a[ok_a], n_a[ok_a], area[ok_a]
    ok_b, on_b, n_b = _on_face(on_a, fb)
    gap = np.full(len(on_a), np.inf)
    gap[ok_b] = np.linalg.norm(on_b[ok_b] - on_a[ok_b], axis=1)
    opposed = np.einsum("ij,ij->i", n_a, n_b) <= -math.cos(math.radians(FACING_DEG))
    ahead = np.einsum("ij,ij->i", on_b - on_a, n_a) >= -tol
    m = ok_b & (gap <= limit) & opposed & ((gap <= tol) | ahead)
    return on_a[m], area[m], gap[m]


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
    from OCP.BRep import BRep_Tool
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.TopAbs import TopAbs_IN, TopAbs_VERTEX
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    t0 = time.time()
    mm = float(mm_per_unit) or 1.0
    tol = max(float(tol), 1e-12)
    out = Measured(tol=tol)
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

    pairs_tried = 0
    for i, j in combinations(range(len(parts)), 2):
        if time.time() - t0 > budget_s:
            out.complete = False
            break
        limit = max(gap_limit_mm(diags[i] * mm, diags[j] * mm) / mm, tol)
        if not _meet(boxes[i], boxes[j], 2 * limit):
            continue
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
        crossing: list[tuple[int, int, list[np.ndarray]]] = []
        for (a, b), dd in sorted(approx.items()):
            pa, aa, ga = _facing(si, a, faces[i][a], sj, b, faces[j][b], limit, reach, tol, fine)
            _pb, ab, _gb = _facing(sj, b, faces[j][b], si, a, faces[i][a], limit, reach, tol, fine)
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
            out.joints[(i, j)] = Joint(i + 1, j + 1, TOUCHING if gap <= tol else GAP, faces_a=fa_keep,
                                       faces_b=fb_keep, area=min(total_a, total_b),
                                       gap=0.0 if gap <= tol else gap,
                                       gap_max=float(g[:, 0].max()) if gap > tol else 0.0,
                                       at=[float(x) for x in where / max(wsum, 1e-300)], limit=limit)
        elif crossing:
            pts = np.vstack([np.array(at) for _a, _b, at in crossing])
            out.joints[(i, j)] = Joint(i + 1, j + 1, OVERLAP, faces_a=sorted({a + 1 for a, _b, _at in crossing}),
                                       faces_b=sorted({b + 1 for _a, b, _at in crossing}),
                                       at=[float(x) for x in pts.mean(axis=0)], limit=limit)
        else:
            out.near[(i, j)] = {"skin_only": skinned}
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
        if jt.kind == GAP:
            row.update(gap_mm=_r(float(jt.gap or 0.0) * scale), gap_max_mm=_r(float(jt.gap_max or 0.0) * scale),
                       gap_limit_mm=_r(jt.limit * scale))
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
        if (i, j) not in measured.joints and measured.complete:
            near = measured.near.get((i, j))
            gap = _dist(_shells(parts[i]), _shells(parts[j]))
            if near and near.get("skin_only"):
                found.append({"kind": "skin_only", "parts": names,
                              "words": f"the fuse joins {names[0]} and {names[1]}, but they meet only on faces "
                                       "that cross their own part"})
            elif gap > tol:
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
