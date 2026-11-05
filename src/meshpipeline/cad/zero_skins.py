# Responsibility: Find the zero-thickness skins of a solid - a face lying on another face of the same solid, the other side out, with no material between them (a hole or pocket cut exactly to a wall, leaving its floor on top of that wall) - and give the mesher a copy of the solid with each skin left out and its outline opened through the face it lies on.
# Boundaries: never alters the CAD file or the staged solid the plan measured: it returns a NEW solid for the mesh, with every face's origin, and keeps the original whenever the opened copy is not a valid closed solid of the same volume. Detect-and-mesh only: repairing the CAD is the CAD repair stage's.
# Collaborates with: cad/fea_assembly.stage (meshes the opened parts, names every face for its origin), cad/fea_features (the measurement reads through the same skins: COINCIDENT_BAND).
"""ZERO-THICKNESS SKINS.

An 11-part bolted assembly's tube (job bf863215, 26 Oct) had four pockets cut from outside down to exactly its
inner radius. Each pocket's floor - a patch of the inner cylinder - was left in the solid, lying ON the tube's
inner wall with the other side out: no material between them, and the inner wall itself never cut. OpenCASCADE
calls the solid valid; no mesher can fill it - gmsh answers "PLC Error: A segment and a facet intersect", alone
and fused, at every size. The material is all there; only the skin is not material.

So the mesher is given the solid with each skin LEFT OUT and its outline opened through the face it lay on: the
pocket goes through, as the CAD's own other pockets do. Nothing with material is touched: the opened solid is
kept only when it is closed, valid, and has the original's volume (to VOLUME_SHARE); otherwise the part is meshed
as drawn and the ladder goes on as before.

A skin B lies on a face A when (all on the exact CAD):
  * A and B belong to the same solid and share no edge;
  * every one of SAMPLES points of B lies on A's surface (within ON_SHARE of the part, or twice the faces'
    tolerance) and inside A's own boundary;
  * their outward normals there are opposite (the material is on both outer sides: none between them).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

#: points of B tested on A (a grid over B's parameter box, kept where they fall inside B)
SAMPLES = 5
#: a point lies ON A within this share of the part's diagonal (or twice the faces' own tolerance)
ON_SHARE = 1e-6
#: normals this close to opposite (cosine)
OPPOSITE_COS = -0.999
#: the opened solid keeps the original's volume to this share (a skin has none)
VOLUME_SHARE = 1e-4
#: a solid of more faces is not searched (the pairs grow as its square; its box test keeps most cheap)
MAX_FACES = 4000


@dataclass
class Skin:
    """Face `skin` (1-based, the solid's MapShapes order) lies on face `on`."""
    skin: int
    on: int


@dataclass
class Opened:
    """The solid to mesh, each of its faces' origin (1-based, the original's order), and what was left out."""
    solid: object
    origin: list[int] = field(default_factory=list)
    skins: list[Skin] = field(default_factory=list)
    note: str = ""


def _faces(solid) -> list:
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(solid, TopAbs_FACE, m)
    return [TopoDS.Face_s(m.FindKey(i)) for i in range(1, m.Extent() + 1)]


def _props(shape, volume: bool) -> tuple[float, tuple[float, float, float]]:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    g = GProp_GProps()
    (BRepGProp.VolumeProperties_s if volume else BRepGProp.SurfaceProperties_s)(shape, g)
    c = g.CentreOfMass()
    return float(g.Mass()), (c.X(), c.Y(), c.Z())


def _normal(face, u: float, v: float):
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepLProp import BRepLProp_SLProps
    from OCP.TopAbs import TopAbs_REVERSED

    props = BRepLProp_SLProps(BRepAdaptor_Surface(face), u, v, 1, 1e-9)
    if not props.IsNormalDefined():
        return None
    n = props.Normal()
    s = -1.0 if face.Orientation() == TopAbs_REVERSED else 1.0
    return (s * n.X(), s * n.Y(), s * n.Z())


def _samples(face) -> list[tuple[float, float]]:
    """(u, v) points spread over the face, inside its own boundary."""
    from OCP.BRepClass import BRepClass_FaceClassifier
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Pnt2d
    from OCP.TopAbs import TopAbs_IN

    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    out = []
    for i in range(SAMPLES):
        for j in range(SAMPLES):
            u = u0 + (u1 - u0) * (i + 0.5) / SAMPLES
            v = v0 + (v1 - v0) * (j + 0.5) / SAMPLES
            if BRepClass_FaceClassifier(face, gp_Pnt2d(u, v), 1e-9).State() == TopAbs_IN:
                out.append((u, v))
    return out


def _shares_edge(a, b) -> bool:
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    ea, eb = TopTools_IndexedMapOfShape(), TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(a, TopAbs_EDGE, ea)
    TopExp.MapShapes_s(b, TopAbs_EDGE, eb)
    return any(ea.Contains(eb.FindKey(i)) for i in range(1, eb.Extent() + 1))


def find(solid) -> list[Skin]:
    """Every skin of `solid` (see the module text). Never raises: a solid that cannot be read has none."""
    try:
        return _find(solid)
    except Exception:  # noqa: BLE001 - no skin found is no skin opened: the part is meshed as drawn
        return []


def _find(solid) -> list[Skin]:
    from OCP.Bnd import Bnd_Box
    from OCP.BRep import BRep_Tool
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.ShapeAnalysis import ShapeAnalysis_Surface

    faces = _faces(solid)
    if len(faces) > MAX_FACES:
        return []
    boxes = []
    for f in faces:
        b = Bnd_Box()
        BRepBndLib.AddOptimal_s(f, b, False, False)
        boxes.append(b)
    whole = Bnd_Box()
    BRepBndLib.AddOptimal_s(solid, whole, False, False)
    x0, y0, z0, x1, y1, z1 = whole.Get()
    diag = math.dist((x0, y0, z0), (x1, y1, z1)) or 1.0
    areas = [_props(f, False)[0] for f in faces]
    out: list[Skin] = []
    for ib, fb in enumerate(faces):
        pts = None
        for ia, fa in enumerate(faces):
            if ia == ib or areas[ia] < areas[ib] or boxes[ia].IsOut(boxes[ib]) or _shares_edge(fa, fb):
                continue
            tol = max(ON_SHARE * diag, 2.0 * max(BRep_Tool.Tolerance_s(fa), BRep_Tool.Tolerance_s(fb)))
            if pts is None:
                pts = _samples(fb)
                if not pts:
                    break
            sb = BRep_Tool.Surface_s(fb)
            sa = ShapeAnalysis_Surface(BRep_Tool.Surface_s(fa))
            ok = True
            for u, v in pts:
                p = sb.Value(u, v)
                uv = sa.ValueOfUV(p, tol)
                if sa.Gap() > tol:
                    ok = False
                    break
                # inside A's own boundary: the exact distance to the bounded face (a periodic surface's
                # parameters need not fall in the face's own range, so its 2D classifier would say OUT)
                d = BRepExtrema_DistShapeShape(BRepBuilderAPI_MakeVertex(p).Vertex(), fa)
                if not d.IsDone() or d.Value() > tol:
                    ok = False
                    break
                na, nb = _normal(fa, uv.X(), uv.Y()), _normal(fb, u, v)
                if na is None or nb is None or sum(x * y for x, y in zip(na, nb, strict=True)) > OPPOSITE_COS:
                    ok = False
                    break
            if ok:
                out.append(Skin(skin=ib + 1, on=ia + 1))
                break
    return out


def open_skins(solid, skins: list[Skin] | None = None) -> Opened:
    """The solid with every skin left out and its outline opened through the face it lies on - or the
    original, unchanged, when there is none or the opened copy is not sound. Never raises."""
    n = len(_faces(solid))
    same = Opened(solid=solid, origin=list(range(1, n + 1)))
    skins = find(solid) if skins is None else skins
    if not skins:
        return same
    try:
        return _open(solid, skins)
    except Exception as exc:  # noqa: BLE001 - meshed as drawn, said
        same.note = f"its skins could not be opened ({type(exc).__name__})"
        return same


def _open(solid, skins: list[Skin]) -> Opened:
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeSolid, BRepBuilderAPI_Sewing
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepLib import BRepLib
    from OCP.BRepTools import BRepTools
    from OCP.ShapeFix import ShapeFix_Face, ShapeFix_Solid
    from OCP.TopAbs import TopAbs_SHELL
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    faces = _faces(solid)
    left_out = {s.skin for s in skins}
    rebuilt: dict[int, object] = {}
    for on in sorted({s.on for s in skins}):
        fa = faces[on - 1]
        mk = BRepBuilderAPI_MakeFace(fa)
        for s in skins:
            if s.on != on:
                continue
            fb = faces[s.skin - 1]
            w = BRepTools.OuterWire_s(fb)
            # B lies on A the other side out, so its outline already runs the way a hole in A runs
            mk.Add(TopoDS.Wire_s(w.Reversed()) if fb.Orientation() == fa.Orientation() else w)
        fix = ShapeFix_Face(mk.Face())
        fix.Perform()
        rebuilt[on] = fix.Face()
    diag = _diag(solid)
    sew = BRepBuilderAPI_Sewing(max(1e-7, 1e-9 * diag))
    for k, f in enumerate(faces, start=1):
        if k not in left_out:
            sew.Add(rebuilt.get(k, f))
    sew.Perform()
    if sew.NbFreeEdges() or sew.NbMultipleEdges():
        raise ValueError(f"the opened shell is not closed ({sew.NbFreeEdges()} free edges)")
    ex = TopExp_Explorer(sew.SewedShape(), TopAbs_SHELL)
    if not ex.More():
        raise ValueError("no shell")
    fixer = ShapeFix_Solid(BRepBuilderAPI_MakeSolid(TopoDS.Shell_s(ex.Current())).Solid())
    fixer.Perform()
    new = TopoDS.Solid_s(fixer.Solid())
    BRepLib.OrientClosedSolid_s(new)
    v0, v1 = _props(solid, True)[0], _props(new, True)[0]
    if not BRepCheck_Analyzer(new).IsValid() or abs(v1 - v0) > VOLUME_SHARE * abs(v0):
        raise ValueError(f"the opened solid is not sound (volume {v1:.6g} for {v0:.6g})")
    # EVERY FACE'S ORIGIN, by area and centre: a face the sewing kept is the original's to the last digit;
    # each rebuilt face is the one with its own surface, the skins' areas cut from it
    old = [(k, *_props(f, False)) for k, f in enumerate(faces, start=1) if k not in left_out]
    want = {k: (a - sum(_props(faces[s.skin - 1], False)[0] for s in skins if s.on == k)) if k in rebuilt else a
            for k, a, _c in old}
    centre = {k: c for k, _a, c in old}
    origin = []
    taken: set[int] = set()
    for f in _faces(new):
        a, c = _props(f, False)
        best = min((k for k in want if k not in taken),
                   key=lambda k: (abs(want[k] - a) / max(abs(a), 1e-30)) + (0.0 if k in rebuilt else
                                                                           math.dist(centre[k], c) / diag))
        taken.add(best)
        origin.append(best)
    if len(origin) != len(old):
        raise ValueError("the opened solid's faces do not match the original's")
    return Opened(solid=new, origin=origin, skins=list(skins))


def _diag(shape) -> float:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    b = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, b, False, False)
    x0, y0, z0, x1, y1, z1 = b.Get()
    return math.dist((x0, y0, z0), (x1, y1, z1)) or 1.0


__all__ = ["OPPOSITE_COS", "ON_SHARE", "Opened", "SAMPLES", "Skin", "VOLUME_SHARE", "find", "open_skins"]
