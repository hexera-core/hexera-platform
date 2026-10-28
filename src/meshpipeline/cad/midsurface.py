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


def mid_faces(fa, fb, t: float, fuzzy: float) -> list:
    """The mid-faces of an even wall pair: both faces offset half the wall inward, and where they overlap."""
    return overlap(half_offset(fa, t / 2.0), half_offset(fb, t / 2.0), fuzzy)


# ------------------------------------------------------------------------------------ extension ----
class _Mid:
    """One mid-face while it is built."""

    def __init__(self, k: int, face, pair: int, body: int, t: float):
        self.k, self.face, self.pair, self.body, self.t = k, face, pair, body, t
        self.plane = _plane(face)
        self.extended: list[dict] = []
        self.original = face
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


def edge_census(fused, owner_t: dict, solid: _Solid | None, *, scale: float, thick_faces: list | None = None) -> dict:
    """The sewn surface's edges: shared by two faces (a closed face's seam counts so), a junction (three or
    more), a real boundary (one face, the outside just past it), a SOLID LINK (one face, the material past it
    belonging to a thick region of the same body - where a mixed part's shell meets its solid) or OPEN (one
    face, still material past it, no thick region there: a junction the mid-faces did not reach). `owner_t`:
    each fused face's wall (its own units) by its index in the fused shape's face map; `scale`: mm per unit;
    `thick_faces`: the body's faces of thick regions (cad/shell_pairs SOLID and THICK roles)."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Curve
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
    thick = _compound(thick_faces) if thick_faces else None
    for i in range(1, e2f.Extent() + 1):
        e = TopoDS.Edge_s(e2f.FindKey(i))
        if BRep_Tool.Degenerated_s(e):
            continue
        faces = []
        for f in e2f.FindFromIndex(i):
            k = fmap.FindIndex(f)
            if k not in faces:
                faces.append(k)
        if len(faces) == 2 or (len(faces) == 1 and BRep_Tool.IsClosed_s(e, TopoDS.Face_s(fmap.FindKey(faces[0])))):
            shared += 1
            continue
        if len(faces) > 2:
            junction += 1
            continue
        face = TopoDS.Face_s(fmap.FindKey(faces[0]))
        t = owner_t.get(faces[0], 0.0)
        p = _edge_mid(e)
        n = _face_normal_at(face, p)
        c = BRepAdaptor_Curve(e)
        from OCP.gp import gp_Pnt, gp_Vec

        q, v = gp_Pnt(), gp_Vec()
        c.D1(0.5 * (c.FirstParameter() + c.LastParameter()), q, v)
        tan = np.array([v.X(), v.Y(), v.Z()])
        if n is None or solid is None or np.linalg.norm(tan) <= 0 or t <= 0:
            boundary += 1
            continue
        d = np.cross(tan / np.linalg.norm(tan), n)
        d /= max(float(np.linalg.norm(d)), 1e-300)
        step = 0.01 * t
        if _in_face(face, p + d * step, 1e-3 * step):
            d = -d
        if not solid.strictly_inside(p + d * OPEN_STEP * t):
            boundary += 1
        elif thick is not None and _dist(_vertex(p), thick) <= (0.5 + OPEN_STEP) * t + solid.tol:
            links.append(_pt(p * scale))
        else:
            open_at.append(_pt(p * scale))
    return {"faces": int(fmap.Extent()), "edges": int(e2f.Extent()), "shared_edges": shared,
            "junction_edges": junction, "boundary_edges": boundary, "solid_link_edges": len(links),
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

    # SHELL-2: every face's role
    widths, radii = [], []
    for k, f in enumerate(faces):
        try:
            widths.append(strip_width(f) * mm)
        except Exception:  # noqa: BLE001
            widths.append(float("inf"))
        radii.append(None if kinds[k] == "plane" else fm.face_radius(f, mm))
    roles = sp.face_roles(names, pairs, kinds=kinds, widths=widths, radii=radii, neighbours=neighbours)
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
        fuzzy = SEW_SHARE * t_min
        solid = _Solid(sol, 1e-3 * t_min)
        for m in mine:
            try:
                extend(m, mine, solid, fuzzy)
            except Exception as exc:  # noqa: BLE001 - a mid-face that cannot be extended keeps its shape
                logger.debug("shell: Mid_%d could not be extended (%s)", m.k + 1, exc)
        if len(mine) == 1:
            fused, images = _compound([mine[0].face]), [[mine[0].face]]
        else:
            try:
                fused, images = sew([m.face for m in mine], fuzzy)
            except ValueError as exc:
                notes.append(f"{body_names[b]}: {exc}.")
                fused, images = _compound([m.face for m in mine]), [[m.face] for m in mine]
        fmap_f = _map(fused, TopAbs_FACE)
        owner_t = {}
        for m, imgs in zip(mine, images, strict=True):
            for f in imgs:
                k = fmap_f.FindIndex(f)
                if k > 0:
                    owner_t[k] = m.t
        thick_faces = [faces[k] for k in range(len(faces))
                       if body_of[k] == b and roles[k]["role"] in (sp.SOLID, sp.THICK)]
        census = edge_census(fused, owner_t, solid, scale=mm, thick_faces=thick_faces)
        census["body"] = body_names[b]
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
        err, npts = fidelity(m.original, faces[p.a], faces[p.b], m.t)
        worst_rel = max(worst_rel, err / m.t if m.t > 0 else 0.0)
        mid_rows.append({"id": f"Mid_{m.k + 1}", "pair": f"P{m.pair + 1}", "body": body_names[m.body],
                         "faces": [names[p.a], names[p.b]], "thickness_mm": _r(p.thickness),
                         "shell": bool(p.shell), "area_mm2": _r(_area(m.original) * mm * mm),
                         "kind": fm._kind_word(BRepAdaptor_Surface(m.original, True).GetType()),
                         "extended": [{"to": f"Mid_{e['to'] + 1}", "by_mm": _r(e["by"] * mm)} for e in m.extended],
                         "fidelity": {"max_error_mm": _r(err * mm), "points": npts}})
    for row, b in zip(bodies, range(len(solids)), strict=True):
        mine = [m for m in mids if m.body == b and pairs[m.pair].shell]
        if mine and row["volume_mm3"]:
            row["mid_volume_ratio"] = _r(sum(_area(m.original) * m.t for m in mine) * mm ** 3 / row["volume_mm3"])
    open_edges = sum(c["open_edges"] for c in sewn_rows)
    accounted = all(roles[k]["role"] in (sp.PAIRED, sp.RIM, sp.BLEND)
                    for k in range(len(faces)) if body_of[k] in shell_bodies)
    vol_ok = all(VOLUME_BAND[0] <= r.get("mid_volume_ratio", 1.0) <= VOLUME_BAND[1]
                 for r in bodies if r["verdict"] == "shell")
    checks = {"fidelity": worst_rel <= FIDELITY_SHARE, "connected": open_edges == 0,
              "faces_accounted": accounted, "volume": vol_ok}
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
              "checks": checks, "complete": True, "notes": notes, "seconds": round(time.time() - started, 2)}
    return record, _compound(surface_faces)


def extract_file(path: Path | str, out_dir: Path | str | None = None, *, mm_per_unit: float) -> dict:
    """Read a STEP, IGES or BREP file, extract its mid-surface, and (when `out_dir` is given) write
    shell.json and midsurface.brep there."""
    from meshpipeline.cad.scout import read_cad

    record, surface = extract(read_cad(Path(path)), mm_per_unit=mm_per_unit)
    if out_dir is not None:
        from OCP.BRepTools import BRepTools

        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        BRepTools.Write_s(surface, str(out / SURFACE_FILE))
        (out / RECORD_FILE).write_text(json.dumps(record, indent=1, sort_keys=True))
    return record


__all__ = ["RECORD_FILE", "SURFACE_FILE", "VERSION", "extract", "extract_file", "mid_faces", "width_of"]
