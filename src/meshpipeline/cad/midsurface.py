# Responsibility: Extract a thin-walled part's MID-SURFACE from its B-rep: each even wall pair's mid-face (the
# overlap of the two faces each offset half the wall inward), each mid-face extended to meet the mid-face it
# runs into at a junction (T, L, cross), all of them sewn into one connected surface per body - and the
# checks that it is faithful (half the wall from both faces, every face accounted for, no open edge inside the
# material). With the record of which body is a shell, which is solid and which is mixed.
# Boundaries: geometry only, deterministic, no model call, no persistence beyond the files it is asked to
# write. It meshes nothing (the shell mesher reads its surface and record). OpenCASCADE throughout: offsets
# (BRepOffsetAPI_MakeOffsetShape, simple mode), Booleans and the general fuse (BOPAlgo_Builder), one thread.
# Collaborates with: cad/shell_pairs.py (the pairs and the face roles), cad/fea_measure.py (the labelled
# surface, Face_k order, face radii), cad/fea_assembly.py (Part_k_Face_j names), docs/design/shell/README.md.
"""THE SHELL RECORD (shell.json) AND THE MID-SURFACE (midsurface.brep).

    {"version": 1, "unit": "mm", "ratio": 0.1, "bodies": [{"body": "Part_1", "verdict": "shell" | "mixed" |
     "solid", "why", "area_mm2", "volume_mm3", "shell_area_share", "mid_volume_ratio", "solid_regions": [...]}],
     "pairs": [{"id": "P1", "faces": [..], "thickness_mm", "spread", "rays", "even", "why", "width_mm",
                "ratio", "shell", "mids": ["Mid_1"]}],
     "faces": [{"face", "kind", "role", "pairs", "body"}...],
     "mids": [{"id": "Mid_1", "pair": "P1", "body": "Part_1", "faces": [..], "thickness_mm", "area_mm2",
               "kind", "extended": [{"to": "Mid_2", "by_mm": ..}], "fidelity": {"max_error_mm", "points"}}],
     "sewn": {"faces", "edges", "shared_edges", "junction_edges", "boundary_edges", "open_edges",
              "open_at_mm": [...]},
     "checks": {"fidelity": bool, "connected": bool, "faces_accounted": bool, "volume": bool},
     "complete": bool, "notes": [...], "seconds"}

midsurface.brep holds one compound: every sewn mid-face, in the order of "surface_faces" in the record (each
entry: the mid-face it came from, so its thickness and its source faces).
"""
from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path

import numpy as np

from meshpipeline.cad import shell_pairs as sp

logger = logging.getLogger(__name__)

VERSION = 1
RECORD_FILE = "shell.json"
SURFACE_FILE = "midsurface.brep"
#: EXTENSION (SHELL-3): a mid-face's straight edge is carried on to the mid-plane it runs into when every one
#: of EXTEND_SAMPLES points along it, moved outward in the face's plane, meets that plane within the reach -
#: half the target's wall plus EXTEND_WALLS times the thicker of the two walls (a fillet up to twice the wall
#: at the junction is bridged) - and the whole way there is inside the part's material. The edge then runs
#: along the line where the two mid-planes meet. Planes within NOT_PARALLEL_DEG of parallel are not met.
EXTEND_SAMPLES = (0.2, 0.5, 0.8)
EXTEND_WALLS = 2.0
NOT_PARALLEL_DEG = 5.0
#: A mid-face edge lying within JOINED_SHARE of a wall from another mid-face already meets it (a sheet's bend:
#: the flange's mid-plane and the bend's mid-cylinder share their edge) and is not extended.
JOINED_SHARE = 1e-3
#: THE SEW (SHELL-4): the mid-faces of one body are fused (general fuse) with this fuzzy tolerance, as a share
#: of the thinnest wall: edges that meet within it become one edge, a face another face's edge lies on is split
#: there - a conformal shell, shared nodes at every junction.
SEW_SHARE = 1e-3
#: ... but never below SEW_TOL_FACTOR times the largest tolerance the mid-faces carry themselves (offset B-spline
#: walls of a lofted cyclone funnel came out of the overlap with 0.0125 mm on 5 mm walls, and a fuzzy value
#: under that broke the general fuse), up to SEW_SHARE_MAX of the thinnest wall
SEW_TOL_FACTOR = 4.0
SEW_SHARE_MAX = 1e-2
#: AN OPEN EDGE: a free edge of the sewn surface (one face) whose outward side, OPEN_STEP of the wall past
#: it, is still inside the material - a junction the mid-faces did not reach. A real boundary (a plate's
#: rim, a hole) has the outside there.
OPEN_STEP = 0.3
#: FIDELITY (SHELL-5): every mid-face point read (FIDELITY_SAMPLES x FIDELITY_SAMPLES over its parameters)
#: lies half the wall from both of its faces within FIDELITY_SHARE of the wall.
FIDELITY_SAMPLES = 5
FIDELITY_SHARE = 0.02
#: THE VOLUME CHECK: a shell body's mid-faces times their walls hold its volume within VOLUME_BAND (rims,
#: blends and the corners of junctions are counted once, or not at all).
VOLUME_BAND = (0.85, 1.10)


def _r(v: float) -> float:
    v = float(v)
    return float(f"{v:.6g}") if math.isfinite(v) else v


def _pt(p) -> list[float]:
    return [_r(x) for x in np.asarray(p, dtype=float)]


# ----------------------------------------------------------------------------------- OCC helpers ----
def _map(shape, kind):
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, kind, m)
    return m


def _faces(shape) -> list:
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopoDS import TopoDS

    m = _map(shape, TopAbs_FACE)
    return [TopoDS.Face_s(m.FindKey(i)) for i in range(1, m.Extent() + 1)]


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


def _length(shape) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    g = GProp_GProps()
    BRepGProp.LinearProperties_s(shape, g)
    return abs(float(g.Mass()))


def _compound(shapes):
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound

    c = TopoDS_Compound()
    b = BRep_Builder()
    b.MakeCompound(c)
    for s in shapes:
        b.Add(c, s)
    return c


def _dist(a, b) -> float:
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape

    d = BRepExtrema_DistShapeShape(a, b)
    return float(d.Value()) if d.IsDone() else float("inf")


def _vertex(p):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    from OCP.gp import gp_Pnt

    return BRepBuilderAPI_MakeVertex(gp_Pnt(*map(float, p))).Vertex()


def width_of(shape) -> float:
    """A face's (or a set of faces') in-plane WIDTH, in its own units: the narrower of the middle side of its
    oriented bounding box and 4 x area / outer perimeter (a strip's width, a square's side; an L-shaped plate
    reads its arms, not its box)."""
    from OCP.Bnd import Bnd_OBB
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepTools import BRepTools

    obb = Bnd_OBB()
    BRepBndLib.AddOBB_s(shape, obb, True, True, False)
    sides = sorted(2.0 * x for x in (obb.XHSize(), obb.YHSize(), obb.ZHSize()))
    area = _area(shape)
    per = sum(_length(BRepTools.OuterWire_s(f)) for f in _faces(shape))
    w = sides[1]
    if per > 0:
        w = min(w, 4.0 * area / per)
    return float(w)


def strip_width(face) -> float:
    """A face's STRIP width, its own units: twice its area over its whole perimeter (holes included) - a
    strip's width, a ring's radial width, a hole's bore depth; never more than its oriented box's middle
    side. A plate's rim reads the plate's thickness."""
    from OCP.Bnd import Bnd_OBB
    from OCP.BRepBndLib import BRepBndLib

    obb = Bnd_OBB()
    BRepBndLib.AddOBB_s(face, obb, True, True, False)
    sides = sorted(2.0 * x for x in (obb.XHSize(), obb.YHSize(), obb.ZHSize()))
    per = _length(face)
    return float(min(sides[1], 2.0 * _area(face) / per)) if per > 0 else float(sides[1])


def _plane(face):
    """(origin, unit normal, x axis, y axis) of a planar face, numpy, the face's own coordinates; None for
    any other surface."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Plane

    a = BRepAdaptor_Surface(face, True)
    if a.GetType() != GeomAbs_Plane:
        return None
    pos = a.Plane().Position()

    def v(d):
        return np.array([d.X(), d.Y(), d.Z()], dtype=float)

    return v(pos.Location()), v(pos.Direction()), v(pos.XDirection()), v(pos.YDirection())


def _in_face(face, p, tol) -> bool:
    from OCP.BRepClass import BRepClass_FaceClassifier
    from OCP.gp import gp_Pnt
    from OCP.TopAbs import TopAbs_IN

    return BRepClass_FaceClassifier(face, gp_Pnt(*map(float, p)), tol).State() == TopAbs_IN


class _Solid:
    """Is a point inside a body (or on its skin)?"""

    def __init__(self, solid, tol: float):
        from OCP.BRepClass3d import BRepClass3d_SolidClassifier

        self.c = BRepClass3d_SolidClassifier(solid)
        self.tol = tol

    def inside(self, p) -> bool:
        from OCP.gp import gp_Pnt
        from OCP.TopAbs import TopAbs_IN, TopAbs_ON

        self.c.Perform(gp_Pnt(*map(float, p)), self.tol)
        return self.c.State() in (TopAbs_IN, TopAbs_ON)

    def strictly_inside(self, p) -> bool:
        from OCP.gp import gp_Pnt
        from OCP.TopAbs import TopAbs_IN

        self.c.Perform(gp_Pnt(*map(float, p)), self.tol)
        return self.c.State() == TopAbs_IN


# ------------------------------------------------------------------------------------ mid-faces ----
def half_offset(face, half: float):
    """The face offset `half` (its own units) INTO the material: OpenCASCADE's simple offset along the
    face's oriented normal (outward), so a negative distance."""
    from OCP.BRepOffsetAPI import BRepOffsetAPI_MakeOffsetShape

    mk = BRepOffsetAPI_MakeOffsetShape()
    mk.PerformBySimple(face, -float(half))
    if not mk.IsDone():
        raise ValueError("the face could not be offset")
    return mk.Shape()


def overlap(a, b, fuzzy: float) -> list:
    """The faces where two faces on one surface overlap (a Boolean common, one thread)."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    from OCP.TopTools import TopTools_ListOfShape

    c = BRepAlgoAPI_Common()
    la, lb = TopTools_ListOfShape(), TopTools_ListOfShape()
    la.Append(a)
    lb.Append(b)
    c.SetArguments(la)
    c.SetTools(lb)
    c.SetFuzzyValue(fuzzy)
    c.SetRunParallel(False)
    c.SetNonDestructive(True)
    c.Build()
    if not c.IsDone():
        return []
    return [f for f in _faces(c.Shape()) if _area(f) > 0]


def toward(face, other, half: float):
    """The face offset `half` toward `other` (its wall partner): the inward offset, checked by distance - the
    simple offset follows the surface's own normal, which is not the face's outward side on every surface (a
    revolved cone's), so the side that lands half the wall from `other` is taken."""
    first = half_offset(face, half)
    if abs(_dist(first, other) - half) <= 0.05 * half:
        return first
    second = half_offset(face, -half)
    return second if abs(_dist(second, other) - half) < abs(_dist(first, other) - half) else first


def mid_faces(fa, fb, t: float, fuzzy: float) -> list:
    """The mid-faces of an even wall pair: both faces offset half the wall toward each other, and where they
    overlap."""
    return overlap(toward(fa, fb, t / 2.0), toward(fb, fa, t / 2.0), fuzzy)


# ------------------------------------------------------------------------------------ extension ----
class _Mid:
    """One mid-face while it is built."""

    def __init__(self, k: int, face, pair: int, body: int, t: float):
        self.k, self.face, self.pair, self.body, self.t = k, face, pair, body, t
        self.plane = _plane(face)
        self.extended: list[dict] = []
        self.original = face
        self.true_face = face            # where the mid-face really lies (the checks read this one)
        self.offset = 0.0                # how far it was moved onto a thicker neighbour's mid-plane (SHELL-7)
        self.box = _box(face)            # the original face's box, its own units
        self.face_box = self.box         # the (extended) face's


def _box(shape) -> tuple[np.ndarray, np.ndarray]:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    b = Bnd_Box()
    BRepBndLib.Add_s(shape, b)
    x0, y0, z0, x1, y1, z1 = b.Get()
    return np.array([x0, y0, z0]), np.array([x1, y1, z1])


def _near_box(p, box, reach: float) -> bool:
    """Is point p within `reach` of the box (a quick test before an exact distance)?"""
    lo, hi = box
    return bool(np.all(p >= lo - reach) and np.all(p <= hi + reach))


def _ordered_edges(wire, face) -> list:
    from OCP.BRepTools import BRepTools_WireExplorer

    out = []
    ex = BRepTools_WireExplorer(wire, face)
    while ex.More():
        out.append(ex.Current())
        ex.Next()
    return out


def _ends(edge):
    from OCP.BRep import BRep_Tool
    from OCP.TopExp import TopExp

    a, b = TopExp.FirstVertex_s(edge, True), TopExp.LastVertex_s(edge, True)

    def xyz(v):
        p = BRep_Tool.Pnt_s(v)
        return np.array([p.X(), p.Y(), p.Z()])

    return xyz(a), xyz(b)


def _is_line(edge) -> bool:
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.GeomAbs import GeomAbs_Line

    return BRepAdaptor_Curve(edge).GetType() == GeomAbs_Line


def _outward(face, p0, p1, n, step) -> np.ndarray | None:
    t = p1 - p0
    L = float(np.linalg.norm(t))
    if L <= 0:
        return None
    d = np.cross(t / L, n)
    d /= max(float(np.linalg.norm(d)), 1e-300)
    mid = 0.5 * (p0 + p1)
    tol = 1e-3 * step
    if _in_face(face, mid + d * step, tol):
        d = -d
    if _in_face(face, mid + d * step, tol) or not _in_face(face, mid - d * step, tol):
        return None
    return d


def _target(m: _Mid, p0, p1, d, mids: list[_Mid], solid: _Solid) -> tuple[_Mid, float] | None:
    """The planar mid-face whose plane every sample of the edge p0-p1 meets, moving along d, inside the
    material and within the reach; and the farthest such distance. None when the samples do not agree."""
    found = []
    for f in EXTEND_SAMPLES:
        p = p0 + (p1 - p0) * f
        best = None
        for o in mids:
            if o is m or o.plane is None or o.body != m.body:
                continue
            on, nn = o.plane[0], o.plane[1]
            cos = float(np.dot(d, nn))
            if abs(float(np.dot(m.plane[1], nn))) > math.cos(math.radians(NOT_PARALLEL_DEG)) or abs(cos) < 1e-6:
                continue
            s = float(np.dot(on - p, nn)) / cos
            reach = 0.5 * o.t + EXTEND_WALLS * max(m.t, o.t)
            if not (1e-9 * reach < s <= reach):
                continue
            if best is not None and s >= best[1] - 1e-9 * reach:
                continue
            if not all(solid.inside(p + d * s * g) for g in (0.25, 0.5, 0.75)):
                continue
            if not _near_box(p + d * s, o.box, reach) or _dist(_vertex(p + d * s), o.original) > reach:
                continue
            best = (o, s)
        if best is None:
            return None
        found.append(best)
    planes = {(tuple(np.round(o.plane[1] * np.sign(o.plane[1][np.argmax(np.abs(o.plane[1]))]), 9)),
               round(float(np.dot(o.plane[0], o.plane[1] * np.sign(o.plane[1][np.argmax(np.abs(o.plane[1]))]))), 9))
              for o, _s in found}
    if len(planes) != 1:
        return None
    return min(found, key=lambda x: x[0].k)[0], max(s for _o, s in found)


def _meet_line(m: _Mid, o: _Mid):
    """The line where two planes meet, in m's plane frame: (point, unit direction), 2D."""
    om, nm, ex, ey = m.plane
    oo, no = o.plane[0], o.plane[1]
    u = np.cross(nm, no)
    u /= np.linalg.norm(u)
    # a point on both planes: the one nearest om
    A = np.array([nm, no, u])
    q = np.linalg.solve(A, np.array([np.dot(nm, om), np.dot(no, oo), np.dot(u, om)]))
    to2 = lambda v: np.array([np.dot(v - om, ex), np.dot(v - om, ey)])  # noqa: E731
    return to2(q), np.array([np.dot(u, ex), np.dot(u, ey)])


def _cross2(a, b) -> float:
    return float(a[0] * b[1] - a[1] * b[0])


def _intersect(l1, l2):
    (p, u), (q, v) = l1, l2
    den = _cross2(u, v)
    if abs(den) < 1e-9:
        return None
    s = _cross2(q - p, v) / den
    return p + u * s


def _foot(x, line):
    p, u = line
    return p + u * float(np.dot(x - p, u))


def extend(m: _Mid, mids: list[_Mid], solid: _Solid, fuzzy: float) -> None:
    """SHELL-3: carry m's straight edges on to the mid-planes they run into (see EXTEND_*). m.face is
    replaced by the extended face; m.extended says which edges went where. An edge already meeting another
    mid-face (JOINED_SHARE) stays. A face that cannot be rebuilt keeps its own shape."""
    if m.plane is None:
        return
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeWire
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Ax3, gp_Dir, gp_Pln, gp_Pnt
    from OCP.TopAbs import TopAbs_WIRE
    from OCP.TopoDS import TopoDS

    om, nm, ex, ey = m.plane
    to2 = lambda v: np.array([np.dot(v - om, ex), np.dot(v - om, ey)])  # noqa: E731
    to3 = lambda w: om + ex * w[0] + ey * w[1]  # noqa: E731
    face = m.face
    outer = BRepTools.OuterWire_s(face)
    wires = [outer] + [TopoDS.Wire_s(w) for w in (_map(face, TopAbs_WIRE).FindKey(i)
                                                    for i in range(1, _map(face, TopAbs_WIRE).Extent() + 1))
                       if not w.IsSame(outer)]
    others = [o for o in mids if o is not m and o.body == m.body]
    changed = False
    new_wires = []
    for wire in wires:
        edges = _ordered_edges(wire, face)
        items = []
        for e in edges:
            p0, p1 = _ends(e)
            item = {"edge": e, "line": _is_line(e), "p0": to2(p0), "p1": to2(p1), "new": None}
            if item["line"]:
                mid3 = 0.5 * (p0 + p1)
                joined = any(_near_box(mid3, o.face_box, JOINED_SHARE * max(m.t, o.t) + fuzzy)
                             and _dist(_vertex(mid3), o.face) <= JOINED_SHARE * max(m.t, o.t) + fuzzy for o in others)
                d = None if joined else _outward(face, p0, p1, nm, 0.01 * m.t)
                hit = _target(m, p0, p1, d, mids, solid) if d is not None else None
                if hit is not None:
                    o, s = hit
                    item["new"] = _meet_line(m, o)
                    item["to"] = o
                    item["by"] = s
                    changed = True
            items.append(item)
        new_wires.append(items)
    if not changed:
        return

    def line_of(it):
        if it["new"] is not None:
            return it["new"]
        u = it["p1"] - it["p0"]
        return it["p0"], u / max(float(np.linalg.norm(u)), 1e-300)

    built = []
    for items in new_wires:
        n = len(items)
        starts: list[np.ndarray] = [np.zeros(2)] * n
        ends: list[np.ndarray] = [np.zeros(2)] * n
        for i in range(n):
            a, b = items[i], items[(i + 1) % n]
            v = a["p1"]                       # the shared vertex (a's end, b's start)
            if a["new"] is None and b["new"] is None:
                ends[i], starts[(i + 1) % n] = v, v
                continue
            if a["line"] and b["line"]:
                x = _intersect(line_of(a), line_of(b))
                if x is not None:
                    ends[i], starts[(i + 1) % n] = x, x
                    continue
            ends[i] = _foot(v, line_of(a)) if a["line"] else v
            starts[(i + 1) % n] = _foot(v, line_of(b)) if b["line"] else v
        mk = BRepBuilderAPI_MakeWire()
        tiny = 1e-7 * max(m.t, 1e-9)
        for i, it in enumerate(items):
            s0, e0 = starts[i], ends[i]
            if it["line"]:
                if it["new"] is not None or np.linalg.norm(s0 - it["p0"]) > tiny or np.linalg.norm(e0 - it["p1"]) > tiny:
                    u0 = it["p1"] - it["p0"]
                    if float(np.dot(e0 - s0, u0)) <= 0:
                        return            # the edge turned round: an extension this simple rule cannot draw
                    mk.Add(BRepBuilderAPI_MakeEdge(gp_Pnt(*to3(s0)), gp_Pnt(*to3(e0))).Edge())
                else:
                    mk.Add(it["edge"])
            else:
                mk.Add(it["edge"])
            nxt = starts[(i + 1) % n]
            if np.linalg.norm(nxt - e0) > tiny:
                mk.Add(BRepBuilderAPI_MakeEdge(gp_Pnt(*to3(e0)), gp_Pnt(*to3(nxt))).Edge())
        if not mk.IsDone():
            return
        built.append(mk.Wire())
    pln = gp_Pln(gp_Ax3(gp_Pnt(*om), gp_Dir(*nm), gp_Dir(*ex)))
    mf = BRepBuilderAPI_MakeFace(pln, built[0], True)
    for w in built[1:]:
        mf.Add(w)
    if not mf.IsDone():
        return
    from OCP.ShapeFix import ShapeFix_Face

    fix = ShapeFix_Face(mf.Face())
    fix.Perform()
    new = fix.Face()
    if not BRepCheck_Analyzer(new).IsValid() or _area(new) < _area(face) * (1 - 1e-9):
        return
    m.face = new
    m.face_box = _box(new)
    for items in new_wires:
        for it in items:
            if it["new"] is not None:
                m.extended.append({"to": it["to"].k, "by": float(it["by"])})


# ------------------------------------------------------------------------------------------ sew ----
def sew(faces: list, fuzzy: float):
    """The general fuse of the faces (one thread): (the fused shape, for each input the faces it became)."""
    from OCP.BOPAlgo import BOPAlgo_Builder

    b = BOPAlgo_Builder()
    for f in faces:
        b.AddArgument(f)
    b.SetFuzzyValue(fuzzy)
    b.SetRunParallel(False)
    b.SetNonDestructive(True)
    b.Perform()
    if b.HasErrors():
        raise ValueError("the mid-faces could not be sewn")
    out = b.Shape()
    images = []
    for f in faces:
        mod = list(b.Modified(f))
        images.append(mod if mod else ([] if b.IsDeleted(f) else [f]))
    return out, images


def _edge_mid(edge):
    from OCP.BRepAdaptor import BRepAdaptor_Curve

    c = BRepAdaptor_Curve(edge)
    u = 0.5 * (c.FirstParameter() + c.LastParameter())
    p = c.Value(u)
    return np.array([p.X(), p.Y(), p.Z()])


def _face_normal_at(face, p):
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepLProp import BRepLProp_SLProps
    from OCP.gp import gp_Pnt
    from OCP.ShapeAnalysis import ShapeAnalysis_Surface

    uv = ShapeAnalysis_Surface(BRep_Tool.Surface_s(face)).ValueOfUV(gp_Pnt(*map(float, p)), 1e-9)
    pr = BRepLProp_SLProps(BRepAdaptor_Surface(face), uv.X(), uv.Y(), 1, 1e-9)
    if not pr.IsNormalDefined():
        return None
    n = pr.Normal()
    return np.array([n.X(), n.Y(), n.Z()])


#: an edge ENDS IN ANOTHER WALL when both it and the point past it lie within this share over half of that
#: wall's thickness from that wall's mid-face
IN_WALL_SLACK = 1.05
#: ... and when the step past it runs along that wall: within 60 degrees of parallel to it
IN_WALL_COS = 0.5


def _sewn_reach(start: int, adjacency: dict[int, set[int]]) -> set[int]:
    """Every sewn face reachable from `start` through shared edges: the connected surface it belongs to."""
    seen, todo = {start}, [start]
    while todo:
        k = todo.pop()
        for j in adjacency.get(k, ()):
            if j not in seen:
                seen.add(j)
                todo.append(j)
    return seen


def _ends_in_wall(p, q, own: int, walls: list, tol: float, sewn_to: set | None = None) -> bool:
    """Do the edge point p and the point q just past it both lie inside the wall of a sewn face that the edge's own
    sheet is sewn to (`sewn_to`: the face indices joined to it through the surface)? Two sheets that never meet -
    a butt step left unsnapped - are a gap, not one wall holding the other."""
    vp = vq = None
    for k, f, w, box in walls:
        if k == own or (sewn_to is not None and k not in sewn_to):
            continue
        reach = 0.5 * w * IN_WALL_SLACK + tol
        if not (_near_box(p, box, reach) and _near_box(q, box, reach)):
            continue
        vp = vp if vp is not None else _vertex(p)
        vq = vq if vq is not None else _vertex(q)
        if _dist(vp, f) <= reach and _dist(vq, f) <= reach:
            n = _face_normal_at(f, p)
            step = q - p
            # it runs ALONG that wall (a plate side inside a thicker plate), not INTO it (a junction stopped short)
            if n is not None and abs(float(np.dot(step / max(float(np.linalg.norm(step)), 1e-300), n))) <= IN_WALL_COS:
                return True
    return False


def _near_any(p, faces_boxes, reach: float) -> bool:
    """Is point p within `reach` of any of the faces (each with its box: boxes first, exact distance after)?"""
    v = None
    for f, box in faces_boxes:
        if _near_box(p, box, reach):
            v = v if v is not None else _vertex(p)
            if _dist(v, f) <= reach:
                return True
    return False


def _inside_edge(edge, face) -> bool:
    """Is the edge inside its one face rather than on its border: a closed surface's seam, or a SLIT (an edge the
    face's boundary runs along on both sides - where the general fuse cut a face along a line that ends inside
    it)? Either way the face is on both sides of it: no free edge."""
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer

    if BRep_Tool.IsClosed_s(edge, face):
        return True
    from OCP.TopAbs import TopAbs_EXTERNAL, TopAbs_INTERNAL

    n = 0
    ex = TopExp_Explorer(face, TopAbs_EDGE)
    while ex.More():
        if ex.Current().IsSame(edge):
            if ex.Current().Orientation() in (TopAbs_INTERNAL, TopAbs_EXTERNAL):
                return True             # the fuse writes a slit once, as an internal edge
            n += 1
        ex.Next()
    return n >= 2


#: where along a free edge its outward side is read: the middle first, then off it (a point on a closed
#: surface's seam, or at a vertex, cannot tell the face's side from the other)
SIDE_FRACTIONS = (0.5, 0.37, 0.63, 0.21, 0.79)


def _outward_at(edge, face, step: float):
    """(a point on the edge, the unit direction in the face's surface away from the face there) at the first
    place along the edge where exactly one side lies in the face; None when no place tells."""
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.gp import gp_Pnt, gp_Vec

    c = BRepAdaptor_Curve(edge)
    a, b = c.FirstParameter(), c.LastParameter()
    for frac in SIDE_FRACTIONS:
        q, v = gp_Pnt(), gp_Vec()
        c.D1(a + (b - a) * frac, q, v)
        p = np.array([q.X(), q.Y(), q.Z()])
        tan = np.array([v.X(), v.Y(), v.Z()])
        n = _face_normal_at(face, p)
        if n is None or np.linalg.norm(tan) <= 0:
            continue
        d = np.cross(tan / np.linalg.norm(tan), n)
        if np.linalg.norm(d) <= 0:
            continue
        d /= float(np.linalg.norm(d))
        plus, minus = _in_face(face, p + d * step, 1e-3 * step), _in_face(face, p - d * step, 1e-3 * step)
        if plus != minus:
            return p, (-d if plus else d)
    return None


def edge_census(fused, owner_t: dict, solid: _Solid | None, *, scale: float, thick_faces: list | None = None) -> dict:
    """The sewn surface's edges: shared by two faces (a closed face's seam counts so), a junction (three or
    more), a real boundary (one face, the outside just past it), a SOLID LINK (one face, the material past it
    belonging to a thick region of the same body - where a mixed part's shell meets its solid) or OPEN (one
    face, still material past it, no thick region there: a junction the mid-faces did not reach). `owner_t`:
    each fused face's wall (its own units) by its index in the fused shape's face map; `scale`: mm per unit;
    `thick_faces`: the body's faces of thick regions (cad/shell_pairs SOLID and THICK roles)."""
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    e2f = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(fused, TopAbs_EDGE, TopAbs_FACE, e2f)
    fmap = _map(fused, TopAbs_FACE)
    shared = junction = boundary = 0
    open_at: list[list[float]] = []
    links: list[list[float]] = []
    thick = [(f, _box(f)) for f in thick_faces] if thick_faces else []
    # every sewn face with its wall: an edge that ends INSIDE ANOTHER SHELL WALL is no open edge
    walls = [(k, TopoDS.Face_s(fmap.FindKey(k)), owner_t.get(k, 0.0)) for k in range(1, fmap.Extent() + 1)]
    walls = [(k, f, w, _box(f)) for k, f, w in walls if w > 0]
    in_wall = 0
    adjacency: dict[int, set[int]] = {}
    for i in range(1, e2f.Extent() + 1):
        ks = {fmap.FindIndex(f) for f in e2f.FindFromIndex(i)} - {0}
        for k in ks:
            adjacency.setdefault(k, set()).update(ks - {k})
    for i in range(1, e2f.Extent() + 1):
        e = TopoDS.Edge_s(e2f.FindKey(i))
        if BRep_Tool.Degenerated_s(e):
            continue
        faces = []
        for f in e2f.FindFromIndex(i):
            k = fmap.FindIndex(f)
            if k not in faces:
                faces.append(k)
        if len(faces) == 2 or (len(faces) == 1 and _inside_edge(e, TopoDS.Face_s(fmap.FindKey(faces[0])))):
            shared += 1
            continue
        if len(faces) > 2:
            junction += 1
            continue
        face = TopoDS.Face_s(fmap.FindKey(faces[0]))
        t = owner_t.get(faces[0], 0.0)
        if solid is None or t <= 0:
            boundary += 1
            continue
        found = _outward_at(e, face, 0.01 * t)
        if found is None:
            boundary += 1
            continue
        p, d = found
        q = p + d * OPEN_STEP * t
        if not solid.strictly_inside(q):
            boundary += 1
        elif _ends_in_wall(p, q, faces[0], walls, solid.tol, sewn_to=_sewn_reach(faces[0], adjacency)):
            # SHELL-3: the edge of a thinner plate's band that runs into a thicker plate's wall and ends there (the
            # thinner plate's own side, inside the junction): the material past it is the other wall, meshed
            in_wall += 1
        elif _near_any(p, thick, (0.5 + OPEN_STEP) * t + solid.tol):
            links.append(_pt(p * scale))
        else:
            open_at.append(_pt(p * scale))
    return {"faces": int(fmap.Extent()), "edges": int(e2f.Extent()), "shared_edges": shared,
            "junction_edges": junction, "boundary_edges": boundary, "ends_in_wall_edges": in_wall,
            "solid_link_edges": len(links),
            "open_edges": len(open_at), "open_at_mm": open_at[:50], "solid_link_at_mm": links[:50]}


# ------------------------------------------------------------------------------------- fidelity ----
def fidelity(mid, fa, fb, t: float) -> tuple[float, int]:
    """The largest distance error of the mid-face from half the wall to each of its faces (its own units),
    over the points read, and how many points."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepClass import BRepClass_FaceClassifier
    from OCP.gp import gp_Pnt2d
    from OCP.TopAbs import TopAbs_IN

    a = BRepAdaptor_Surface(mid, True)
    u0, u1, v0, v1 = a.FirstUParameter(), a.LastUParameter(), a.FirstVParameter(), a.LastVParameter()
    worst, n = 0.0, 0
    k = FIDELITY_SAMPLES
    for i in range(k):
        for j in range(k):
            u = u0 + (u1 - u0) * (i + 0.5) / k
            v = v0 + (v1 - v0) * (j + 0.5) / k
            if BRepClass_FaceClassifier(mid, gp_Pnt2d(u, v), 1e-9).State() != TopAbs_IN:
                continue
            p = a.Value(u, v)
            vx = _vertex((p.X(), p.Y(), p.Z()))
            worst = max(worst, abs(_dist(vx, fa) - t / 2), abs(_dist(vx, fb) - t / 2))
            n += 1
    return worst, n


# -------------------------------------------------------------------------------------- extract ----
# ------------------------------------------------------------------------- join: grow, cut, keep ----
#: SHELL-3 (v2, 29 Oct): every shell mid-face of a body is GROWN on its own surface by GROW_REACH (half the body's
#: thickest wall plus GROW_WALLS times its own wall) past every edge; the grown faces, the mid-faces themselves and
#: the body's own CAD faces are fused (general fuse, one thread); and each mid-face keeps, besides itself, the
#: grown pieces that (a) lie inside the part's material, (b) are reached from it without crossing another
#: mid-face, and (c) reach another mid-face. So a mid-face runs on until it meets the mid-face it runs into -
#: whatever the two surfaces are: plane to plane (T, L, cross, box), cylinder to plane (a tube on a plate),
#: cylinder to cone, a flange that faces only part of an edge (a relief cut) - and stops there; past a plate's
#: rim or into a hole it is outside the material and is dropped; into a thick region with no mid-face to meet
#: (a mixed part's solid) it is dropped too, and the edge there is a solid link.
GROW_WALLS = 2.0


def joined_edges(m: _Mid, others: list[_Mid], fuzzy: float, *, curved_only: bool = False) -> list:
    """The edges of mid-face m that already meet another mid-face of its body (a flange running into its bend,
    two pieces of one plate side by side): within JOINED_SHARE of a wall of it."""
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopoDS import TopoDS

    out = []
    em = _map(m.original, TopAbs_EDGE)
    for i in range(1, em.Extent() + 1):
        e = TopoDS.Edge_s(em.FindKey(i))
        p = _edge_mid(e)
        for o in others:
            if o is m or (curved_only and o.plane is not None):
                continue
            reach = JOINED_SHARE * max(m.t, o.t) + fuzzy
            if _near_box(p, o.box, reach) and _dist(_vertex(p), o.original) <= reach:
                out.append(e)
                break
    return out


def grow(face, reach: float, joined: list | None = None):
    """The face grown `reach` (its own units) past its edges on its own surface; None when it cannot be grown.
    A plane grows by offsetting its outline outward and its holes inward (sharp corners kept); any other
    surface by widening its parameter range by `reach` measured at the face's middle (a periodic direction that
    already closes stays closed). It never grows past a JOINED edge (one that already meets another mid-face):
    a band laid tangent along a bend it already meets would only spoil that shared edge."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Pnt, gp_Vec

    surf = BRep_Tool.Surface_s(face)
    joined = joined or []
    if _plane(face) is not None:
        grown = _grow_plane(face, reach, joined)
        if grown is not None:
            return grown
        # the outline offset failed (a disc bounded by one circle did): grow its parameter range as tiles instead
    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    hold = _held_sides(face, joined, (u0, u1, v0, v1))
    a = BRepAdaptor_Surface(face, True)
    p, du_vec, dv_vec = gp_Pnt(), gp_Vec(), gp_Vec()
    a.D1(0.5 * (u0 + u1), 0.5 * (v0 + v1), p, du_vec, dv_vec)
    su, sv = du_vec.Magnitude(), dv_vec.Magnitude()
    if su <= 0 or sv <= 0:
        return None
    du, dv = reach / su, reach / sv
    for name, per in (("u", surf.UPeriod() if surf.IsUPeriodic() else None),
                      ("v", surf.VPeriod() if surf.IsVPeriodic() else None)):
        if per:
            span = (u1 - u0) if name == "u" else (v1 - v0)
            cap = max(0.0, min(0.25 * per, 0.45 * (per - span)))
            if name == "u":
                du = min(du, cap)
            else:
                dv = min(dv, cap)
    lo_u = u0 if "u0" in hold else u0 - du
    hi_u = u1 if "u1" in hold else u1 + du
    lo_v = v0 if "v0" in hold else v0 - dv
    hi_v = v1 if "v1" in hold else v1 + dv
    if surf.IsUPeriodic() and hi_u - lo_u >= surf.UPeriod():
        lo_u, hi_u = u0, u0 + surf.UPeriod()
    if surf.IsVPeriodic() and hi_v - lo_v >= surf.VPeriod():
        lo_v, hi_v = v0, v0 + surf.VPeriod()
    b = surf.Bounds()
    short = {name for name, want, have, up in (("u0", lo_u, b[0], False), ("u1", hi_u, b[1], True),
                                               ("v0", lo_v, b[2], False), ("v1", hi_v, b[3], True))
             if not (surf.IsUPeriodic() if name[0] == "u" else surf.IsVPeriodic())
             and (want > have if up else want < have)}
    if short:
        surf = _extended_surface(face, surf, GROW_EXTEND * reach, short, (u0, u1, v0, v1))
        b = surf.Bounds()
    if not surf.IsUPeriodic():
        lo_u, hi_u = max(lo_u, b[0]), min(hi_u, b[1])
    if not surf.IsVPeriodic():
        lo_v, hi_v = max(lo_v, b[2]), min(hi_v, b[3])
    # as TILES - the face's own range and a band on each grown side - so no one face runs across a closed
    # surface's seam (the general fuse could not split a grown torus that did, and lost the whole mid-face)
    us = [lo_u, u0, u1, hi_u]
    vs = [lo_v, v0, v1, hi_v]
    tiles = []
    for a0, a1 in zip(us[:-1], us[1:], strict=True):
        for b0, b1 in zip(vs[:-1], vs[1:], strict=True):
            if a1 - a0 <= 1e-12 or b1 - b0 <= 1e-12:
                continue
            mk = BRepBuilderAPI_MakeFace(surf, a0, a1, b0, b1, 1e-7)
            if mk.IsDone():
                tiles.append(mk.Face())
    return _compound(tiles) if tiles else None


#: a surface that ends where its face does (a lofted B-spline wall, its offset) is carried on past its own ends by
#: this share of the reach before the face is grown on it, so it can reach the wall it runs into
GROW_EXTEND = 1.5


def _extended_surface(face, surf, length: float, sides: set[str], bounds):
    """The face's surface carried on `length` (its own units) past each of its own parameter ends in `sides`
    (OCC's BRepLib::ExtendFace: a B-spline or Bezier continued smoothly, an offset surface by its basis), or the
    surface itself when it cannot be: the extension must keep the face's own points where they are."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepLib import BRepLib
    from OCP.TopoDS import TopoDS_Face

    try:
        out = TopoDS_Face()
        BRepLib.ExtendFace_s(face, float(length), "u0" in sides, "u1" in sides, "v0" in sides, "v1" in sides, out)
        if out.IsNull():
            return surf
        ext = BRep_Tool.Surface_s(out)
        u0, u1, v0, v1 = bounds
        scale = max(length, 1e-12)
        for fu, fv in ((0.5, 0.5), (0.0, 0.0), (1.0, 1.0), (0.0, 1.0), (1.0, 0.0)):
            u, v = u0 + fu * (u1 - u0), v0 + fv * (v1 - v0)
            if surf.Value(u, v).Distance(ext.Value(u, v)) > 1e-6 * scale:
                return surf
        return ext
    except Exception as exc:  # noqa: BLE001 - the surface is grown as far as it goes
        logger.debug("shell: a surface could not be extended (%s)", exc)
        return surf


def _held_sides(face, joined: list, bounds) -> set[str]:
    """Which sides of a face's parameter rectangle (u0, u1, v0, v1) a joined edge lies on: those are not grown."""
    from OCP.BRep import BRep_Tool
    from OCP.gp import gp_Pnt
    from OCP.ShapeAnalysis import ShapeAnalysis_Surface

    u0, u1, v0, v1 = bounds
    surf = BRep_Tool.Surface_s(face)
    sa = ShapeAnalysis_Surface(surf)
    eu, ev = 1e-6 * max(abs(u1 - u0), 1e-12), 1e-6 * max(abs(v1 - v0), 1e-12)
    pu = surf.UPeriod() if surf.IsUPeriodic() else None
    pv = surf.VPeriod() if surf.IsVPeriodic() else None

    def near(val: float, ref: float, eps: float, period: float | None) -> bool:
        d = val - ref
        if period:
            d = (d + 0.5 * period) % period - 0.5 * period
        return abs(d) <= max(eps, 1e-9)

    out: set[str] = set()
    for e in joined:
        uv = sa.ValueOfUV(gp_Pnt(*map(float, _edge_mid(e))), 1e-7)
        for name, val, ref, eps, per in (("u0", uv.X(), u0, eu, pu), ("u1", uv.X(), u1, eu, pu),
                                         ("v0", uv.Y(), v0, ev, pv), ("v1", uv.Y(), v1, ev, pv)):
            if near(val, ref, eps, per):
                out.add(name)
    return out


def _plane_union(a, b):
    """The union of two faces on one plane, as one face (the larger piece when it comes apart)."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
    from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain

    f = BRepAlgoAPI_Fuse(a, b)
    if not f.IsDone():
        return a
    u = ShapeUpgrade_UnifySameDomain(f.Shape(), True, True, True)
    u.Build()
    fs = _faces(u.Shape())
    return _compound(fs) if fs else a


def _cut_beyond(grown, face, joined: list, reach: float):
    """The grown plane with the strip beyond each joined straight edge taken off (a 2D cut in the plane)."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    from OCP.gp import gp_Pnt

    pl = _plane(face)
    if pl is None:
        return grown
    out = grown
    for e in joined:
        if not _is_line(e):
            continue
        p0, p1 = _ends(e)
        d = _outward(face, p0, p1, pl[1], 1e-3 * reach)
        if d is None:
            continue
        poly = BRepBuilderAPI_MakePolygon()
        for q in (p0, p1, p1 + d * 1.01 * reach, p0 + d * 1.01 * reach):
            poly.Add(gp_Pnt(*map(float, q)))
        poly.Close()
        strip = BRepBuilderAPI_MakeFace(BRep_Tool.Surface_s(face), poly.Wire(), True)
        if not strip.IsDone():
            continue
        cut = BRepAlgoAPI_Cut(out, strip.Face())
        if cut.IsDone():
            fs = _faces(cut.Shape())
            if fs:
                out = _compound(fs)          # every piece: a strip can part the band in two
    return out


#: an offset outline whose tolerance is above this share of the reach is rebuilt from its edges' 3D curves
GROW_TOL_SHARE = 1e-4


def _max_tol(shape) -> float:
    from OCP.ShapeAnalysis import ShapeAnalysis_ShapeTolerance
    from OCP.TopAbs import TopAbs_SHAPE

    return float(ShapeAnalysis_ShapeTolerance().Tolerance(shape, 1, TopAbs_SHAPE))


def _clean_wire(wire, reach: float):
    """The offset outline, or the same outline rebuilt edge by edge from its 3D curves when OCC's 2D offset left
    it a wild tolerance (a hole's circle came back with a tolerance of 138 mm on a plate whose plane is placed at
    its corner, and that one wire broke the general fuse of the whole body); None when it cannot be mended."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeWire
    from OCP.BRepTools import BRepTools_WireExplorer
    from OCP.TopoDS import TopoDS

    limit = GROW_TOL_SHARE * reach
    if _max_tol(wire) <= limit:
        return wire
    mw = BRepBuilderAPI_MakeWire()
    ex = BRepTools_WireExplorer(wire)
    while ex.More():
        e = ex.Current()
        if not BRep_Tool.Degenerated_s(e):
            first, last = BRep_Tool.Range_s(e)
            curve = BRep_Tool.Curve_s(e, 0.0, 1.0)
            if curve is None:
                return None
            mk = BRepBuilderAPI_MakeEdge(curve, first, last)
            if not mk.IsDone():
                return None
            # the wire runs the same way round (an outline one way, a hole the other)
            mw.Add(TopoDS.Edge_s(mk.Edge().Oriented(e.Orientation())))
            if not mw.IsDone():
                return None
        ex.Next()
    if not mw.IsDone():
        return None
    out = TopoDS.Wire_s(mw.Wire().Oriented(wire.Orientation()))
    return out if _max_tol(out) <= limit else None


def _outer_wire(face):
    from OCP.BRepTools import BRepTools

    return BRepTools.OuterWire_s(face)


def _inner_wires(face) -> list:
    """The face's wires other than its outline (its holes), in the face's own order."""
    from OCP.TopAbs import TopAbs_WIRE
    from OCP.TopoDS import TopoDS

    outer = _outer_wire(face)
    wm = _map(face, TopAbs_WIRE)
    return [TopoDS.Wire_s(wm.FindKey(i)) for i in range(1, wm.Extent() + 1) if not wm.FindKey(i).IsSame(outer)]


def _face_with_holes(surf, outline, holes: list):
    """A face on the plane `surf` bounded by `outline` with `holes` cut out, each hole run the other way round
    from the outline (one that would ADD area is turned round), or None."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.TopoDS import TopoDS

    if outline is None or outline.IsNull():
        return None
    mf = BRepBuilderAPI_MakeFace(surf, outline, True)
    if not mf.IsDone():
        return None
    face = mf.Face()
    if _signed_area(face) < 0:
        face = BRepBuilderAPI_MakeFace(surf, TopoDS.Wire_s(outline.Reversed()), True).Face()
    whole = _signed_area(face)
    for w in holes:
        for cand in (w, TopoDS.Wire_s(w.Reversed())):
            mk = BRepBuilderAPI_MakeFace(face)
            mk.Add(cand)
            if mk.IsDone() and 0 <= _signed_area(mk.Face()) < whole:
                face = mk.Face()
                whole = _signed_area(face)
                break
        else:
            return None
    return face


def _signed_area(face) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    g = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, g)
    return float(g.Mass())


def _left_over(face, grown) -> float:
    """The area of `face` not covered by `grown` (both on one plane)."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut

    cut = BRepAlgoAPI_Cut(face, grown)
    if not cut.IsDone():
        return float("inf")
    return sum(_area(f) for f in _faces(cut.Shape()))


def _offset_region(surf, wire, dist: float, join) -> list:
    """The region inside `wire` (on the plane `surf`) offset by `dist` (outward when positive, inward when
    negative), as faces - the largest first; [] when nothing is left (a hole shrunk away) or it cannot be done.
    OCC's 2D offset takes its sign from how the wire runs against the plane's normal, so the sign is checked
    on the result: an offset that went the wrong way (a 20 mm hole of a base plate grew to 30 mm when asked
    to shrink to 10) is done again the other way."""
    from OCP.BRepOffsetAPI import BRepOffsetAPI_MakeOffset
    from OCP.TopAbs import TopAbs_WIRE
    from OCP.TopoDS import TopoDS

    region = _face_with_holes(surf, wire, [])
    if region is None:
        return []
    whole = _area(region)
    for signed in (dist, -dist):
        mk = BRepOffsetAPI_MakeOffset(region, join)
        mk.Perform(signed)
        if not mk.IsDone():
            if signed == dist:
                continue
            return []
        wm = _map(mk.Shape(), TopAbs_WIRE)
        out = []
        for i in range(1, wm.Extent() + 1):
            w = _clean_wire(TopoDS.Wire_s(wm.FindKey(i)), abs(dist))
            f = _face_with_holes(surf, w, []) if w is not None else None
            if f is not None and _area(f) > 0:
                out.append(f)
        total = sum(_area(f) for f in out)
        if (total > whole) if dist > 0 else (total < whole):
            out.sort(key=lambda f: -_area(f))
            return out
    return []


def _grow_plane(face, reach: float, joined: list | None = None):
    """A plane face grown `reach` past its edges in its own plane: its outline offset outward and each hole
    offset inward on its own (a hole that shrinks away is gone), the shrunk holes cut from the grown outline.
    Each wire is offset alone: OCC's 2D offset of a whole face with holes grew a 20 mm hole of a bolted base
    plate to 30 mm instead of 10 mm, and left another hole's circle a 138 mm tolerance."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.GeomAbs import GeomAbs_Arc, GeomAbs_Intersection
    from OCP.ShapeFix import ShapeFix_Face

    surf = BRep_Tool.Surface_s(face)
    holes = _inner_wires(face)
    for join in (GeomAbs_Intersection, GeomAbs_Arc):
        try:
            grown = _offset_region(surf, _outer_wire(face), reach, join)
            if not grown:
                continue
            out = grown[0]
            for w in holes:
                for left in _offset_region(surf, w, -reach, join):
                    cut = BRepAlgoAPI_Cut(out, left)
                    if not cut.IsDone():
                        raise ValueError("a shrunk hole could not be cut out")
                    fs = _faces(cut.Shape())
                    if not fs:
                        raise ValueError("a shrunk hole took the whole face")
                    out = _compound(fs) if len(fs) > 1 else fs[0]
            if len(_faces(out)) == 1:
                fix = ShapeFix_Face(_faces(out)[0])
                fix.Perform()
                out = fix.Face()
            if joined:
                out = _cut_beyond(out, face, joined, reach)
                out = _plane_union(out, face)
            # the grown face must hold the whole face it was grown from
            if _area(out) >= _area(face) * (1 - 1e-9) and _left_over(face, out) <= 1e-6 * _area(face):
                return out
        except Exception as exc:  # noqa: BLE001 - try the other join, then give up growing this face
            logger.debug("shell: a plane could not be grown (%s)", exc)
    return None


#: the deflections (shares of the face's box diagonal) _inner_point meshes a face at, coarse first
INNER_MESH_SHARES = (0.05, 0.005, 0.0005)
#: ... and how many of each mesh's largest triangles it tries for a middle inside the face
INNER_TRIES = 64


def _inner_point(face):
    """A point inside the face (its own units), or None: the middle of the largest triangle of the face's own
    triangulation (UV coordinates, so on the surface exactly) - inside any face, however thin (a 1.5 mm band
    along a 200 mm edge has no point of a coarse parameter grid in it). A curved sliver the coarse mesher
    cannot triangulate (a 1 mm band along a 550 mm curved edge) is meshed again finer. The point must lie IN the
    face: a coarse mesh cuts across a curved border (a 2 mm ring round a 40 mm hole, meshed at a 9 mm sag), so a
    triangle's middle can fall outside it; the largest triangle whose middle is in the face is taken, finer
    meshes after."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepClass import BRepClass_FaceClassifier
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.gp import gp_Pnt2d
    from OCP.TopAbs import TopAbs_IN
    from OCP.TopLoc import TopLoc_Location

    lo, hi = _box(face)
    size = float(np.linalg.norm(hi - lo)) or 1.0
    fallback = None
    for share in INNER_MESH_SHARES:
        BRepMesh_IncrementalMesh(face, share * size, False, 0.5, False)
        tri = BRep_Tool.Triangulation_s(face, TopLoc_Location())
        if tri is None or tri.NbTriangles() == 0 or not tri.HasUVNodes():
            continue
        sized = []
        for k in range(1, tri.NbTriangles() + 1):
            a, b, c = tri.Triangle(k).Get()
            pa, pb, pc = tri.UVNode(a), tri.UVNode(b), tri.UVNode(c)
            A = abs((pb.X() - pa.X()) * (pc.Y() - pa.Y()) - (pc.X() - pa.X()) * (pb.Y() - pa.Y()))
            sized.append((-A, k, ((pa.X() + pb.X() + pc.X()) / 3.0, (pa.Y() + pb.Y() + pc.Y()) / 3.0)))
        sized.sort()
        if fallback is None:
            fallback = sized[0][2]
        for _a, _k, uv in sized[:INNER_TRIES]:
            if BRepClass_FaceClassifier(face, gp_Pnt2d(*uv), 1e-9).State() == TopAbs_IN:
                q = BRepAdaptor_Surface(face, False).Value(*uv)
                return np.array([q.X(), q.Y(), q.Z()])
    if fallback is None:
        return None
    q = BRepAdaptor_Surface(face, False).Value(*fallback)
    return np.array([q.X(), q.Y(), q.Z()])


def join_body(mine: list[_Mid], body_faces: list, solid: _Solid, *, t_max: float, fuzzy: float):
    """SHELL-3 v2 for one body: (the fused shape, the kept faces, each kept face's mid-face index into `mine`,
    for each mid-face the extensions it kept as [(other mid index or None, area)])."""
    from OCP.BOPAlgo import BOPAlgo_Builder
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    grown = []
    for m in mine:
        g = grow(m.original, 0.5 * t_max + GROW_WALLS * m.t, joined_edges(m, mine, fuzzy, curved_only=m.plane is not None))
        grown.append(g)
    b = BOPAlgo_Builder()
    for m in mine:
        b.AddArgument(m.original)
    for g in grown:
        for tile in (_faces(g) if g is not None else []):
            b.AddArgument(tile)
    for f in body_faces:
        b.AddArgument(f)
    b.SetFuzzyValue(fuzzy)
    b.SetRunParallel(False)
    b.SetNonDestructive(True)
    b.Perform()
    if b.HasErrors():
        raise ValueError("the grown mid-faces could not be fused")
    fused = b.Shape()
    fmap = _map(fused, TopAbs_FACE)
    n = fmap.Extent()
    owners: list[set[int]] = [set() for _ in range(n + 1)]
    core: list[set[int]] = [set() for _ in range(n + 1)]
    skin = [False] * (n + 1)          # a piece of the part's own CAD faces

    # WHICH FACE EACH PIECE IS A PIECE OF, by geometry (a point inside the piece, lying in that face): the fuse's
    # own history loses a face it merges with a coincident one (a torus mid-face and its grown copy that wraps
    # round the seam came back "deleted")
    tol = 10.0 * fuzzy + 1e-9
    sources = [(i, "core", m.original, m.box) for i, m in enumerate(mine)]
    sources += [(i, "grown", tile, _box(tile)) for i, g in enumerate(grown) if g is not None for tile in _faces(g)]
    sources += [(-1, "skin", f, _box(f)) for f in body_faces]
    from OCP.TopoDS import TopoDS as _T

    for k in range(1, n + 1):
        q = _inner_point(_T.Face_s(fmap.FindKey(k)))
        if q is None:
            continue
        for i, what, f, box in sources:
            if not _near_box(q, box, tol) or not _in_face(f, q, tol) or _dist(_vertex(q), f) > tol:
                continue
            if what == "skin":
                skin[k] = True
            else:
                owners[k].add(i)
                if what == "core":
                    core[k].add(i)
    e2f = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(fused, TopAbs_EDGE, TopAbs_FACE, e2f)
    face_edges: list[list[int]] = [[] for _ in range(n + 1)]
    edge_faces: list[list[int]] = [[]]
    for ei in range(1, e2f.Extent() + 1):
        fs = sorted({fmap.FindIndex(f) for f in e2f.FindFromIndex(ei)} - {0})
        edge_faces.append(fs)
        for k in fs:
            face_edges[k].append(ei)
    inside: dict[int, bool] = {}

    def in_material(k: int) -> bool:
        if k not in inside:
            from OCP.TopoDS import TopoDS

            p = _inner_point(TopoDS.Face_s(fmap.FindKey(k)))
            inside[k] = p is not None and solid.strictly_inside(p)
        return inside[k]

    def blocked(ei: int, i: int) -> bool:
        return any(owners[h] and i not in owners[h] for h in edge_faces[ei])

    kept_ext: dict[int, set[int]] = {}
    for i in range(len(mine)):
        start = [k for k in range(1, n + 1) if i in core[k]]
        seen = set(start)
        todo = list(start)
        ext: set[int] = set()
        while todo:
            k = todo.pop()
            for ei in face_edges[k]:
                if blocked(ei, i):
                    continue
                for g in edge_faces[ei]:
                    if g in seen or i not in owners[g] or core[g]:
                        continue
                    seen.add(g)
                    if in_material(g):
                        ext.add(g)
                        todo.append(g)
        # (c): keep the pieces of an extension that reach another mid-face, or run to the part's own skin (a wall
        # whose mid-face stopped short of its end); a stub ending inside the material (a thick region) is dropped
        comps, left = [], set(ext)
        while left:
            s0 = min(left)
            comp, stack = {s0}, [s0]
            left.discard(s0)
            while stack:
                k = stack.pop()
                for ei in face_edges[k]:
                    for g in edge_faces[ei]:
                        if g in left:
                            left.discard(g)
                            comp.add(g)
                            stack.append(g)
            comps.append(comp)
        keep: set[int] = set()
        for comp in comps:
            meets = any(blocked(ei, i) and any(owners[h] and i not in owners[h] and (core[h] or in_material(h))
                                               for h in edge_faces[ei])
                        for k in comp for ei in face_edges[k])
            meets = meets or any(any(skin[h] for h in edge_faces[ei]) and not any(i in core[h] for h in edge_faces[ei])
                                 for k in comp for ei in face_edges[k])
            if meets:
                keep |= comp
        kept_ext[i] = keep
    chosen: dict[int, int] = {}
    for k in range(1, n + 1):
        if core[k]:
            chosen[k] = min(core[k])
    for i in sorted(kept_ext):
        for k in sorted(kept_ext[i]):
            chosen.setdefault(k, i)
    from OCP.TopoDS import TopoDS

    kept = [TopoDS.Face_s(fmap.FindKey(k)) for k in sorted(chosen)]
    owner = [chosen[k] for k in sorted(chosen)]
    reached: list[list[tuple[int | None, float]]] = [[] for _ in mine]
    for i, ks in kept_ext.items():
        for k in sorted(ks):
            others = sorted({o for ei in face_edges[k] for h in edge_faces[ei] for o in owners[h]} - {i})
            reached[i].append((others[0] if others else None, _area(TopoDS.Face_s(fmap.FindKey(k)))))
    return _compound(kept), kept, owner, reached


def _face_bodies(shape, faces_map) -> tuple[list[int], list]:
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    smap = _map(shape, TopAbs_SOLID)
    solids = [TopoDS.Solid_s(smap.FindKey(i)) for i in range(1, smap.Extent() + 1)]
    body = [-1] * faces_map.Extent()
    for b, s in enumerate(solids):
        ex = TopExp_Explorer(s, TopAbs_FACE)
        while ex.More():
            k = faces_map.FindIndex(ex.Current())
            if k > 0 and body[k - 1] < 0:
                body[k - 1] = b
            ex.Next()
    return body, solids


def _neighbours(shape, faces_map) -> list[set[int]]:
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    e2f = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, e2f)
    out: list[set[int]] = [set() for _ in range(faces_map.Extent())]
    for i in range(1, e2f.Extent() + 1):
        ks = {faces_map.FindIndex(f) - 1 for f in e2f.FindFromIndex(i)} - {-1}
        for a in ks:
            out[a] |= ks - {a}
    return out


def _regions(faces: list[int], neighbours: list[set[int]]) -> list[list[int]]:
    left, out = set(faces), []
    for f in sorted(faces):
        if f not in left:
            continue
        group, todo = [], [f]
        left.discard(f)
        while todo:
            x = todo.pop()
            group.append(x)
            for y in sorted(neighbours[x] & left):
                left.discard(y)
                todo.append(y)
        out.append(sorted(group))
    return out


#: SHELL-6, NEVER FAIL: a region the mid-faces do not close - the part's faces within FALLBACK_WALLS times the
#: thicker wall there of an open edge - is meshed SOLID and tied to the shell round it, and said so. A body the
#: extraction cannot read at all is meshed solid whole. The record never stops a run.
FALLBACK_WALLS = 3.0


#: SHELL-7, PLATES OF TWO THICKNESSES MEETING IN LINE (a butt step, flush on one side): their mid-planes are
#: parallel and apart by half the difference, so nothing meets. The thinner plate's mid-face is moved onto the
#: thicker one's mid-plane - when the two are parallel within STEP_PARALLEL_DEG, apart by no more than half the
#: thicker wall, and touch edge to edge (the faces come within the offset plus STEP_TOUCH of the thicker wall:
#: a plate crossing right at the step parts them by its own wall) - and the offset is kept for the solver (Nastran ZOFFS, Abaqus / CalculiX OFFSET): the same stiffness and mass,
#: one flat shell. The checks still read where the mid-face really lies.
STEP_PARALLEL_DEG = 1.0
STEP_TOUCH = 0.5


def _join_straight(mine: list[_Mid], solid: _Solid, fuzzy: float, notes: list[str], body: str):
    """The straight-edge extension (extend) and a plain sew: (fused shape, each mid-face's faces in it)."""
    for m in mine:
        m.face = m.original
        try:
            extend(m, mine, solid, fuzzy)
        except Exception as exc:  # noqa: BLE001 - a mid-face that cannot be extended keeps its shape
            logger.debug("shell: Mid_%d could not be extended (%s)", m.k + 1, exc)
    if len(mine) == 1:
        return _compound([mine[0].face]), [[mine[0].face]]
    try:
        return sew([m.face for m in mine], fuzzy)
    except ValueError as exc:
        notes.append(f"{body}: {exc}.")
        return _compound([m.face for m in mine]), [[m.face] for m in mine]


def _join_general(mine: list[_Mid], body_faces: list, solid: _Solid, *, t_max: float, fuzzy: float):
    """The general join (join_body) and one more sew of what it kept: (fused shape, each mid-face's faces)."""
    fused, kept, owner_i, reached = join_body(mine, body_faces, solid, t_max=t_max, fuzzy=fuzzy)
    for i, m in enumerate(mine):
        for other, area in reached[i]:
            m.extended.append({"to": None if other is None else mine[other].k, "area": area})
    images = [[f for f, o in zip(kept, owner_i, strict=True) if o == i] for i in range(len(mine))]
    if len(kept) > 1:
        fused, again = sew(kept, fuzzy)
        images = [[g for f, o in zip(kept, owner_i, strict=True) if o == i for g in again[kept.index(f)]]
                  for i in range(len(mine))]
    return fused, images


def snap_steps(mine: list[_Mid], solid: _Solid) -> None:
    """SHELL-7 on one body's mid-faces, thickest first (a plate snaps to a neighbour already settled)."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Trsf, gp_Vec

    cos_lim = math.cos(math.radians(STEP_PARALLEL_DEG))
    order = sorted(mine, key=lambda m: (-m.t, m.k))
    for m in order:
        if m.plane is None:
            continue
        best = None
        for o in order:
            if o is m or o.plane is None or o.t <= m.t * (1 + 1e-6):
                continue
            no = o.plane[1]
            if abs(float(np.dot(m.plane[1], no))) < cos_lim:
                continue
            d = float(np.dot(o.plane[0] - m.plane[0], no))      # m's plane to o's plane, along o's normal
            if abs(d) <= 1e-9 * o.t or abs(d) > 0.5 * o.t * (1 + 1e-6):
                continue
            if not _near_box(m.box[0], (o.box[0], o.box[1]), abs(d) + o.t + max(m.box[1] - m.box[0])):
                continue
            if _dist(m.original, o.original) > abs(d) + STEP_TOUCH * o.t:
                continue
            if best is None or o.t > best[0].t:
                best = (o, d)
        if best is None:
            continue
        o, d = best
        tr = gp_Trsf()
        tr.SetTranslation(gp_Vec(*map(float, o.plane[1] * d)))
        moved = BRepBuilderAPI_Transform(m.original, tr, True).Shape()
        moved_face = _faces(moved)[0]
        q = _inner_point(moved_face)
        if q is None or not solid.inside(q):
            continue
        m.original, m.face = moved_face, moved_face
        m.plane = _plane(moved_face)
        m.box = m.face_box = _box(moved_face)
        m.offset = -d                         # the true mid-face lies this far from the shell's reference plane


def fallback_regions(sewn_rows: list[dict], faces: list, names: list[str], body_of: list[int],
                     body_names: list[str], pairs: list, mm: float) -> list[dict]:
    """SHELL-6: one region per group of open edges of a body (open points within FALLBACK_WALLS walls of each
    other share a region): the CAD faces round them, where they are, and why."""
    out: list[dict] = []
    for row in sewn_rows:
        pts = [np.asarray(q, dtype=float) / mm for q in row.get("open_at_mm", [])]
        if not pts:
            continue
        b = body_names.index(row["body"])
        walls = [pr.thickness for pr in pairs if pr.shell and body_of[pr.a] == b]
        reach = FALLBACK_WALLS * (max(walls) if walls else 0.0) / mm
        mine = [(k, _box(faces[k])) for k in range(len(faces)) if body_of[k] == b]
        groups: list[list[np.ndarray]] = []
        for q in pts:
            for g in groups:
                if any(np.linalg.norm(q - r) <= reach for r in g):
                    g.append(q)
                    break
            else:
                groups.append([q])
        for g in groups:
            near = sorted({k for q in g for k, box in mine
                           if _near_box(q, box, reach) and _dist(_vertex(q), faces[k]) <= reach})
            out.append({"body": row["body"], "faces": [names[k] for k in near], "at_mm": [_pt(q * mm) for q in g],
                        "why": "the mid-faces do not meet here; this region is meshed solid and tied to the shell"})
    return out


def extract(shape, *, mm_per_unit: float, names: list[str] | None = None) -> tuple[dict, object]:
    """The shell record and the sewn mid-surface (a compound, the shape's own units) of an OpenCASCADE shape
    whose coordinates are `mm_per_unit` millimetres each. Never raises for a part it cannot shell: the record
    says what was not shelled and why."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.TopAbs import TopAbs_FACE

    from meshpipeline.cad import fea_measure as fm

    started = time.time()
    mm = float(mm_per_unit)
    fmap = _map(shape, TopAbs_FACE)
    faces = _faces(shape)
    body_of, solids = _face_bodies(shape, fmap)
    if names is None:
        if len(solids) > 1:
            from meshpipeline.cad.fea_assembly import global_face_names

            names = global_face_names(shape)
        else:
            names = [f"Face_{k}" for k in range(1, len(faces) + 1)]
    body_names = [f"Part_{b + 1}" for b in range(len(solids))] or ["Part_1"]
    notes: list[str] = []
    surface, _f, kinds = fm._brep_surface(shape, mm)
    partners = sp.ray_partners(surface)
    diag_mm = float(surface.diag)

    # SHELL-1: the even wall pairs, confirmed on the exact faces (read from the smaller face)
    pairs: list[sp.Pair] = []
    for (a, b), rays in partners.items():
        if body_of[a] != body_of[b] or body_of[a] < 0:
            continue
        small, big = (a, b) if _area(faces[a]) <= _area(faces[b]) else (b, a)
        try:
            ok, t, spread, why = sp.even_wall(faces[small], faces[big], mm)
        except Exception as exc:  # noqa: BLE001 - a pair that cannot be read is no pair
            logger.debug("shell: pair %s/%s could not be read (%s)", names[a], names[b], exc)
            ok, t, spread, why = False, float("nan"), float("nan"), "the faces could not be read"
        pairs.append(sp.Pair(a=a, b=b, thickness=t, spread=spread, rays=rays, even=ok, why=why))

    # the mid-faces of every even pair, and each pair's width and verdict (SHELL-2)
    mids: list[_Mid] = []
    for i, p in enumerate(pairs):
        if not p.even:
            continue
        t_u = p.thickness / mm
        try:
            got = mid_faces(faces[p.a], faces[p.b], t_u, 1e-4 * t_u)
        except Exception as exc:  # noqa: BLE001
            logger.debug("shell: the mid-face of %s/%s failed (%s)", names[p.a], names[p.b], exc)
            got = []
        if not got:
            p.why = "the two faces do not overlap"
            continue
        for f in got:
            p.mids.append(len(mids))
            mids.append(_Mid(len(mids), f, i, body_of[p.a], t_u))
    neighbours = _neighbours(shape, fmap)
    # SHELL-2: shell or solid, sheet by sheet
    for s_i, group in enumerate(sp.sheets(pairs, neighbours, body_of)):
        sheet_faces = [mids[k].face for i in group for k in pairs[i].mids]
        size = math.sqrt(sum(_area(f) for f in sheet_faces)) * mm
        width = width_of(_compound(sheet_faces)) * mm
        t = max(pairs[i].thickness for i in group)
        shell = t <= sp.SHELL_RATIO * size and t <= sp.SHELL_WIDTH_RATIO * width
        for i in group:
            pairs[i].sheet, pairs[i].size, pairs[i].width, pairs[i].shell = s_i, size, width, shell
    # SHELL-8: a lap (two shell plates stacked and fused) is shelled with the plates it is made of
    for i in sp.laps(pairs):
        pairs[i].shell = True
        pairs[i].why = "a lap: two shell plates stacked, shelled with them"

    # SHELL-2: every face's role
    widths, radii, arcs = [], [], []
    for k, f in enumerate(faces):
        try:
            widths.append(strip_width(f) * mm)
        except Exception:  # noqa: BLE001
            widths.append(float("inf"))
        per = _length(f)
        arcs.append(2.0 * _area(f) / per * mm if per > 0 else float("inf"))
        radii.append(None if kinds[k] == "plane" else fm.face_radius(f, mm))
    roles = sp.face_roles(names, pairs, kinds=kinds, widths=widths, radii=radii, neighbours=neighbours,
                          arcs=arcs)
    for k, row in enumerate(roles):
        row["body"] = body_names[body_of[k]] if body_of[k] >= 0 else None

    # the body verdicts
    bodies = []
    shell_bodies: set[int] = set()
    for b, sol in enumerate(solids):
        ks = [k for k in range(len(faces)) if body_of[k] == b]
        area = sum(_area(faces[k]) for k in ks) * mm * mm
        shellish = [k for k in ks if roles[k]["role"] in (sp.PAIRED, sp.RIM, sp.BLEND)]
        solid_faces = [k for k in ks if roles[k]["role"] in (sp.SOLID, sp.THICK)]
        has_shell = any(roles[k]["role"] == sp.PAIRED for k in ks)
        share = sum(_area(faces[k]) for k in shellish) * mm * mm / area if area > 0 else 0.0
        if has_shell and not solid_faces:
            verdict, why = "shell", "every wall is thin against its size"
            shell_bodies.add(b)
        elif has_shell:
            verdict, why = "mixed", "thin walls and thick regions"
        else:
            verdict, why = "solid", "no wall is thin against its size"
        row = {"body": body_names[b], "verdict": verdict, "why": why, "area_mm2": _r(area),
               "volume_mm3": _r(_volume(sol) * mm ** 3), "shell_area_share": _r(share)}
        if verdict == "mixed":
            row["solid_regions"] = [{"faces": [names[k] for k in reg],
                                     "area_mm2": _r(sum(_area(faces[k]) for k in reg) * mm * mm)}
                                    for reg in _regions(solid_faces, neighbours)]
        bodies.append(row)

    # SHELL-3 / SHELL-4: extend and sew the shell mid-faces of every shell or mixed body
    surface_faces: list = []
    surface_owner: list[int] = []
    sewn_rows = []
    for b, sol in enumerate(solids):
        mine = [m for m in mids if m.body == b and pairs[m.pair].shell]
        if not mine:
            continue
        t_min = min(m.t for m in mine)
        fuzzy = min(max(SEW_SHARE * t_min, SEW_TOL_FACTOR * max(_max_tol(m.original) for m in mine)),
                    SEW_SHARE_MAX * t_min)
        solid = _Solid(sol, 1e-3 * t_min)
        body_faces = [faces[k] for k in range(len(faces)) if body_of[k] == b]
        t_max = max(pairs[i].thickness for i in range(len(pairs))
                    if pairs[i].even and body_of[pairs[i].a] == b and pairs[i].shell) / mm
        snap_steps(mine, solid)
        thick_faces = [faces[k] for k in range(len(faces))
                       if body_of[k] == b and roles[k]["role"] in (sp.SOLID, sp.THICK)]

        def census_of(fused, images, mine=mine, solid=solid, thick_faces=thick_faces):
            fmap_f = _map(fused, TopAbs_FACE)
            owner_t = {}
            for m, imgs in zip(mine, images, strict=True):
                for f in imgs:
                    k = fmap_f.FindIndex(f)
                    if k > 0:
                        owner_t[k] = m.t
            return edge_census(fused, owner_t, solid, scale=mm, thick_faces=thick_faces)

        # SHELL-3, BOTH JOINS, THE BETTER KEPT: the straight-edge extension (plane to plane: T, L, box, the bent and
        # slotted sheets of the corpus) and the general join (grow, cut, keep: any surfaces - a tube on a plate,
        # a cylinder into a cone, a relief-cut flange). The one leaving fewer open edges is kept; on a tie, the
        # straight one. Deterministic: each is.
        straight = _join_straight(mine, solid, fuzzy, notes, body_names[b])
        census = census_of(*straight)
        chosen, method, ext_straight = straight, "straight", [list(m.extended) for m in mine]
        if census["open_edges"]:
            for m in mine:
                m.extended = []
            try:
                general = _join_general(mine, body_faces, solid, t_max=t_max, fuzzy=fuzzy)
                census_g = census_of(*general)
                if census_g["open_edges"] < census["open_edges"]:
                    chosen, method, census = general, "general", census_g
            except Exception as exc:  # noqa: BLE001 - the straight join stands
                logger.debug("shell: the general join failed on %s (%s)", body_names[b], exc)
            if method == "straight":
                for m, ext in zip(mine, ext_straight, strict=True):
                    m.extended = ext
        fused, images = chosen
        census["body"] = body_names[b]
        census["join"] = method
        sewn_rows.append(census)
        for m, imgs in zip(mine, images, strict=True):
            for f in imgs:
                surface_faces.append(f)
                surface_owner.append(m.k)

    # SHELL-5: the checks
    mid_rows = []
    worst_rel = 0.0
    for m in mids:
        p = pairs[m.pair]
        err, npts = fidelity(m.true_face, faces[p.a], faces[p.b], m.t)
        worst_rel = max(worst_rel, err / m.t if m.t > 0 else 0.0)
        mid_rows.append({"id": f"Mid_{m.k + 1}", "pair": f"P{m.pair + 1}", "body": body_names[m.body],
                         "faces": [names[p.a], names[p.b]], "thickness_mm": _r(p.thickness),
                         "shell": bool(p.shell), "area_mm2": _r(_area(m.true_face) * mm * mm),
                         **({"offset_mm": _r(m.offset * mm)} if m.offset else {}),
                         "kind": fm._kind_word(BRepAdaptor_Surface(m.original, True).GetType()),
                         "extended": [{"to": None if e["to"] is None else f"Mid_{e['to'] + 1}",
                                       **({"by_mm": _r(e["by"] * mm)} if "by" in e else {}),
                                       **({"area_mm2": _r(e["area"] * mm * mm)} if "area" in e else {})}
                                      for e in m.extended],
                         "fidelity": {"max_error_mm": _r(err * mm), "points": npts}})
    for row, b in zip(bodies, range(len(solids)), strict=True):
        mine = [m for m in mids if m.body == b and pairs[m.pair].shell]
        if mine and row["volume_mm3"]:
            row["mid_volume_ratio"] = _r(sum(_area(m.true_face) * m.t for m in mine) * mm ** 3 / row["volume_mm3"])
    open_edges = sum(c["open_edges"] for c in sewn_rows)
    accounted = all(roles[k]["role"] in (sp.PAIRED, sp.RIM, sp.BLEND)
                    for k in range(len(faces)) if body_of[k] in shell_bodies)
    vol_ok = all(VOLUME_BAND[0] <= r.get("mid_volume_ratio", 1.0) <= VOLUME_BAND[1]
                 for r in bodies if r["verdict"] == "shell")
    checks = {"fidelity": worst_rel <= FIDELITY_SHARE, "connected": open_edges == 0,
              "faces_accounted": accounted, "volume": vol_ok}
    fallback = fallback_regions(sewn_rows, faces, names, body_of, body_names, pairs, mm)
    if fallback:
        notes.append(f"{len(fallback)} region(s) where the mid-faces do not meet are meshed solid and tied to the "
                     "shell there (SHELL-6).")
    loose = sum(1 for k in range(len(faces)) if body_of[k] < 0)
    if loose:
        notes.append(f"{loose} face(s) belong to no solid body (a sheet beside the bodies); they are not shelled.")
    record = {"version": VERSION, "unit": "mm", "ratio": sp.SHELL_RATIO, "diag_mm": _r(diag_mm),
              "bodies": bodies,
              "pairs": [{"id": f"P{i + 1}", "faces": [names[p.a], names[p.b]], "thickness_mm": _r(p.thickness),
                         "spread": _r(p.spread), "rays": p.rays, "even": p.even, "why": p.why,
                         "sheet": None if p.sheet is None else f"S{p.sheet + 1}",
                         "size_mm": None if p.size is None else _r(p.size),
                         "width_mm": None if p.width is None else _r(p.width),
                         "ratio": None if not p.size else _r(p.thickness / p.size), "shell": p.shell,
                         "mids": [f"Mid_{k + 1}" for k in p.mids]} for i, p in enumerate(pairs)],
              "faces": roles, "mids": mid_rows, "sewn": sewn_rows,
              "surface_faces": [f"Mid_{k + 1}" for k in surface_owner],
              "checks": checks, "fallback_regions": fallback, "complete": True, "notes": notes,
              "seconds": round(time.time() - started, 2)}
    return record, _compound(surface_faces)


def solid_record(shape, why: str) -> dict:
    """SHELL-6: the record of a part that is meshed solid whole - every body solid, with the reason."""
    from OCP.TopAbs import TopAbs_SOLID

    n = _map(shape, TopAbs_SOLID).Extent()
    return {"version": VERSION, "unit": "mm", "ratio": sp.SHELL_RATIO,
            "bodies": [{"body": f"Part_{b + 1}", "verdict": "solid", "why": why} for b in range(max(n, 1))],
            "pairs": [], "faces": [], "mids": [], "sewn": [], "surface_faces": [],
            "checks": {"fidelity": True, "connected": True, "faces_accounted": True, "volume": True},
            "fallback_regions": [], "complete": False, "notes": [f"The whole part is meshed solid: {why}."],
            "seconds": 0.0}


def extract_file(path: Path | str, out_dir: Path | str | None = None, *, mm_per_unit: float) -> dict:
    """Read a STEP, IGES or BREP file, extract its mid-surface, and (when `out_dir` is given) write
    shell.json and midsurface.brep there."""
    from meshpipeline.cad.scout import read_cad

    shape = read_cad(Path(path))
    try:
        record, surface = extract(shape, mm_per_unit=mm_per_unit)
    except Exception as exc:  # noqa: BLE001 - SHELL-6: never fail; the whole part is meshed solid and told so
        logger.warning("shell: the extraction failed (%s); the part is meshed solid", exc)
        record, surface = solid_record(shape, f"the mid-surface could not be extracted ({type(exc).__name__})"), \
            _compound([])
    if out_dir is not None:
        from OCP.BRepTools import BRepTools

        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        BRepTools.Write_s(surface, str(out / SURFACE_FILE))
        (out / RECORD_FILE).write_text(json.dumps(record, indent=1, sort_keys=True))
    return record


__all__ = ["RECORD_FILE", "SURFACE_FILE", "VERSION", "extract", "extract_file", "mid_faces", "width_of"]
