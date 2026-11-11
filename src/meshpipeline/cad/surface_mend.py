# Responsibility: SURFACES MADE TO MEET (SURF-12): on a surface file's sheets (the surface route, no solid), each place
# where the surfaces do not meet (cad/surface_model.open_places: an edge running alongside another face, 0.4 to a few
# mm off) is closed the way a pre-processor user closes it by hand - the edge MOVED onto its neighbour (a step or a
# corner off its neighbour's: the two faces sewn alone at the gap), the face EXTENDED along its own surface to its
# neighbour (an edge stopping short), or the face TRIMMED back to its neighbour's edge (an overlap) - one place at a
# time, each change judged before it is kept and told: what was done, where, by how much.
# Boundaries: geometry only, deterministic, no model call. Only open sheets read by cad/surface_model (the surface
# route): solids and the extraction from solids are never touched, nor cad/repair (a closed skin's defects are CAD
# repair's). A change never moves geometry farther than the reach (REACH_PART of the part, REACH_SELF of the width
# of the face changed or one element the user asked for, or the user's own "close gaps up to 4 mm"); an extension continues the face's own surface, a trim
# cuts where the faces cross, and a result face must be valid, keep its normal side, keep its area within the band
# the change can explain, and cross no other face. Anything else is left open and told.
# Collaborates with: cad/surface_model.py (open places, the Sewn), cad/surface_clean.py (the pair sewing, the free
# edges), engines/surface_run.py (says what was done).
from __future__ import annotations

import logging
import math
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

#: SURF-12: a change never moves geometry farther than this share of the part's size (1/100: the reach inside which
#: an edge is an open place, SURF-8), nor than what the user's notes allow
REACH_PART = 1e-2
#: ... nor than REACH_SELF of the width of the face that is changed, or the element size the user asked for when that is
#: more (a gap under one element is one an engineer closes by hand). The first version capped it at a quarter of the
#: NARROWER of the two faces: a 5 mm fillet patch beside the main panel held every change on the panel to 1.2 mm.
REACH_SELF = 0.5
#: an extension runs this much past the gap, so the extended face crosses its neighbour and is trimmed there
EXTEND_PAST = 1.5
#: two faces are the same surface continued (a gap in one sheet of metal, not a corner) when their normals at the gap
#: agree within this many degrees: there the extension stops AT the neighbour's edge instead of crossing it
SAME_SIDE_DEG = 15.0
#: points along an edge for the checks
SAMPLES = 7
#: the open places SURF-12 looks at: as SURF-8 counts them (1/100 of the part), or farther when the notes allow
OPEN_REACH_LOOK = 100.0


def _nearest(face, p) -> np.ndarray:
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape

    from meshpipeline.cad import midsurface as ms

    d = BRepExtrema_DistShapeShape(ms._vertex(p), face)
    q = d.PointOnShape2(1)
    return np.array([q.X(), q.Y(), q.Z()])


def _r(v: float) -> float:
    v = float(v)
    return float(f"{v:.6g}") if math.isfinite(v) else v


def _normal_at(face, p) -> np.ndarray | None:
    """The face's material-side normal (its orientation applied) at the point of it nearest p."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepLProp import BRepLProp_SLProps
    from OCP.gp import gp_Pnt
    from OCP.ShapeAnalysis import ShapeAnalysis_Surface
    from OCP.TopAbs import TopAbs_REVERSED

    uv = ShapeAnalysis_Surface(BRep_Tool.Surface_s(face)).ValueOfUV(gp_Pnt(*map(float, p)), 1e-7)
    pr = BRepLProp_SLProps(BRepAdaptor_Surface(face, False), uv.X(), uv.Y(), 1, 1e-9)
    if not pr.IsNormalDefined():
        return None
    n = pr.Normal()
    v = np.array([n.X(), n.Y(), n.Z()])
    return -v if face.Orientation() == TopAbs_REVERSED else v


def _check(new, old, band: float, others: list, tol: float) -> str:
    """SURF-12: the result face judged: '' when it is sound, else why not. Valid; its normal side kept (at a point
    inside the old face); its area within `band` of the old; it crosses none of `others` (beyond touching)."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Section
    from OCP.BRepCheck import BRepCheck_Analyzer

    from meshpipeline.cad import midsurface as ms

    if new is None or new.IsNull() or not BRepCheck_Analyzer(new).IsValid():
        return "the changed face would not be valid"
    if abs(ms._area(new) - ms._area(old)) > band:
        return "the changed face's area would change by more than the gap explains"
    q = ms._inner_point(old)
    if q is not None:
        n0, n1 = _normal_at(old, q), _normal_at(new, q)
        if n0 is None or n1 is None or float(np.dot(n0, n1)) < 0.9:
            return "the changed face would turn"
    lo, hi = ms._box(new)
    for f in others:
        flo, fhi = ms._box(f)
        if np.any(flo > hi + tol) or np.any(lo > fhi + tol):
            continue
        sec = BRepAlgoAPI_Section(new, f)
        sec.Build()
        if sec.IsDone() and _crossing(sec.Shape(), new, f, tol):
            return "the changed face would cross another face"
    return ""


def _crossing(section, a, b, tol: float) -> bool:
    """Whether two faces' section holds a curve that runs through the inside of either (a crossing), not only along
    the edges they share or touch at."""
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad import surface_model as sm

    em = ms._map(section, TopAbs_EDGE)
    for i in range(1, em.Extent() + 1):
        e = TopoDS.Edge_s(em.FindKey(i))
        if ms._length(e) <= 2.0 * tol:
            continue
        mid = sm._edge_points(e, 3)[1]
        on_rim = all(min(ms._dist(ms._vertex(mid), TopoDS.Edge_s(x)) for x in _edges(f)) <= 2.0 * tol for f in (a, b))
        if not on_rim:
            return True
    return False


def _edges(face) -> list:
    from OCP.TopAbs import TopAbs_EDGE

    from meshpipeline.cad import midsurface as ms

    m = ms._map(face, TopAbs_EDGE)
    return [m.FindKey(i) for i in range(1, m.Extent() + 1)]


def _iso_side(face, edge) -> tuple[bool, bool] | None:
    """Which side of the face's surface patch an edge is: (in U, after), when the face is the natural patch of its
    surface (its one wire is the patch's four sides) and the edge lies along one of them; else None."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Pnt
    from OCP.ShapeAnalysis import ShapeAnalysis_Surface
    from OCP.TopAbs import TopAbs_WIRE

    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad import surface_model as sm

    if ms._map(face, TopAbs_WIRE).Extent() != 1:
        return None
    surf = BRep_Tool.Surface_s(face)
    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    whole = BRepBuilderAPI_MakeFace(surf, u0, u1, v0, v1, 1e-7).Face()
    if abs(ms._area(whole) - ms._area(face)) > 1e-6 * ms._area(face):
        return None
    sas = ShapeAnalysis_Surface(surf)
    uvs = np.array([(lambda w: (w.X(), w.Y()))(sas.ValueOfUV(gp_Pnt(*map(float, p)), 1e-7))
                    for p in sm._edge_points(edge, SAMPLES)])
    du, dv = max(u1 - u0, 1e-300), max(v1 - v0, 1e-300)
    for in_u, after, col, bound, span in ((True, True, 0, u1, du), (True, False, 0, u0, du),
                                          (False, True, 1, v1, dv), (False, False, 1, v0, dv)):
        if np.all(np.abs(uvs[:, col] - bound) <= 1e-5 * span):
            return in_u, after
    return None


def _extended(face, edge, length: float, fit: float = 0.0):
    """SURF-12: the face continued past `edge` by `length` along its own surface, as one face; None when the face is
    no natural patch or the extension cannot be made exactly enough.
    - On a plane, cylinder, cone, sphere or torus: the same surface over a wider stretch of its own parameters (the
      surface's own continuation, exact).
    - On a B-spline: the patch rebuilt through its own points and, past the edge, points continued from the edge with
      the surface's first and second derivatives (as OpenCASCADE's GeomLib.ExtendSurfByLength continues it); kept only
      when the rebuilt patch lies on the old one within a tenth of the sew tolerance everywhere it was."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.BRepTools import BRepTools
    from OCP.Geom import (
        Geom_BSplineSurface,
        Geom_ConicalSurface,
        Geom_CylindricalSurface,
        Geom_Plane,
        Geom_RectangularTrimmedSurface,
        Geom_SphericalSurface,
        Geom_ToroidalSurface,
    )
    from OCP.gp import gp_Pnt, gp_Vec
    from OCP.TopAbs import TopAbs_REVERSED
    from OCP.TopoDS import TopoDS

    if length <= 0:
        return None
    side = _iso_side(face, edge)
    if side is None:
        # a TRIMMED face (a bead or a dart cut out of a larger patch): its surface already runs on past the edge
        return _extended_trimmed(face, edge, length)
    in_u, after = side
    surf = BRep_Tool.Surface_s(face)
    while isinstance(surf, Geom_RectangularTrimmedSurface):
        surf = surf.BasisSurface()
    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    # how far in the parameter the length is, read at the edge's middle
    um, vm = (u1 if after else u0, 0.5 * (v0 + v1)) if in_u else (0.5 * (u0 + u1), v1 if after else v0)
    p, du, dv = gp_Pnt(), gp_Vec(), gp_Vec()
    surf.D1(um, vm, p, du, dv)
    speed = (du if in_u else dv).Magnitude()
    if speed <= 1e-300:
        return None
    step = float(length) / speed
    try:
        if isinstance(surf, (Geom_Plane, Geom_CylindricalSurface, Geom_ConicalSurface, Geom_SphericalSurface,
                             Geom_ToroidalSurface)):
            if in_u:
                u0, u1 = (u0, u1 + step) if after else (u0 - step, u1)
            else:
                v0, v1 = (v0, v1 + step) if after else (v0 - step, v1)
            if (surf.IsUPeriodic() and in_u and u1 - u0 >= surf.UPeriod()) or (
                    surf.IsVPeriodic() and not in_u and v1 - v0 >= surf.VPeriod()):
                return None
            out = BRepBuilderAPI_MakeFace(surf, u0, u1, v0, v1, 1e-7).Face()
        elif isinstance(surf, Geom_BSplineSurface):
            out = _extended_bspline(surf, (u0, u1, v0, v1), in_u, after, step, fit)
            if out is None:
                return None
        else:
            return None
    except Exception as exc:  # noqa: BLE001 - not extended: the place is told
        logger.debug("surface mend: a face was not extended (%s)", type(exc).__name__)
        return None
    if face.Orientation() == TopAbs_REVERSED:
        out = TopoDS.Face_s(out.Reversed())
    return out


def _extended_trimmed(face, edge, length: float):
    """SURF-12: a trimmed face continued past one of its edges by `length` on its own surface: a strip of that
    surface between the edge and the edge moved outward (in the surface's own parameters, as far as `length` is
    there), joined to the face into one face (OpenCASCADE's fuse, then same-surface faces unified). None when the
    surface does not run on that far, or the result is not one face of the face's area plus the strip's."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Curve2d, BRepAdaptor_Surface
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeWire
    from OCP.BRepClass import BRepClass_FaceClassifier
    from OCP.BRepLib import BRepLib
    from OCP.GCE2d import GCE2d_MakeSegment
    from OCP.gp import gp_Pnt, gp_Pnt2d, gp_Vec, gp_Vec2d
    from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
    from OCP.TopAbs import TopAbs_FORWARD, TopAbs_OUT, TopAbs_REVERSED
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad import midsurface as ms

    try:
        fwd = TopoDS.Face_s(face.Oriented(TopAbs_FORWARD))
        surf = BRep_Tool.Surface_s(fwd)
        c2 = BRepAdaptor_Curve2d(edge, fwd)
        t0, t1 = c2.FirstParameter(), c2.LastParameter()
        tm = 0.5 * (t0 + t1)
        uv, duv = gp_Pnt2d(), gp_Vec2d()
        c2.D1(tm, uv, duv)
        if duv.Magnitude() < 1e-14:
            return None
        perp = np.array([-duv.Y(), duv.X()]) / duv.Magnitude()
        # outward: the side of the edge that is not the face
        span = max(1e-9, max(abs(c2.Value(t1).X() - c2.Value(t0).X()), abs(c2.Value(t1).Y() - c2.Value(t0).Y())))
        probe = 1e-3 * span
        tol = BRep_Tool.Tolerance_s(fwd)
        outside = None
        for sgn in (1.0, -1.0):
            q = gp_Pnt2d(uv.X() + sgn * probe * perp[0], uv.Y() + sgn * probe * perp[1])
            if BRepClass_FaceClassifier(fwd, q, tol).State() == TopAbs_OUT:
                outside = sgn
                break
        if outside is None:
            return None
        d = outside * perp
        p, su, sv = gp_Pnt(), gp_Vec(), gp_Vec()
        BRepAdaptor_Surface(fwd, False).D1(uv.X(), uv.Y(), p, su, sv)
        speed = float(np.linalg.norm(d[0] * np.array([su.X(), su.Y(), su.Z()]) + d[1] * np.array([sv.X(), sv.Y(), sv.Z()])))
        if speed <= 1e-300:
            return None
        reach_uv = float(length) / speed
        pc = BRep_Tool.CurveOnSurface_s(edge, fwd, 0.0, 0.0)
        if pc is None:
            return None
        a0, a1 = c2.Value(t0), c2.Value(t1)
        # the strip's sides CONTINUE THE FACE'S OWN NEIGHBOURING EDGES past the edge's ends (a rim stays a straight
        # rim), straight out from the edge where a neighbouring edge runs nearly along it
        b0 = _corner(fwd, edge, a0, d, reach_uv)
        b1 = _corner(fwd, edge, a1, d, reach_uv)
        # the surface must run on that far (a patch cut from a larger one does)
        u0, u1, v0, v1 = surf.Bounds()
        for w in (b0, b1):
            if not (u0 - 1e-9 <= w.X() <= u1 + 1e-9 and v0 - 1e-9 <= w.Y() <= v1 + 1e-9):
                return None
        e_in = BRepBuilderAPI_MakeEdge(pc, surf, t0, t1).Edge()
        e_out = BRepBuilderAPI_MakeEdge(GCE2d_MakeSegment(b0, b1).Value(), surf).Edge()
        s0 = BRepBuilderAPI_MakeEdge(GCE2d_MakeSegment(a0, b0).Value(), surf).Edge()
        s1 = BRepBuilderAPI_MakeEdge(GCE2d_MakeSegment(a1, b1).Value(), surf).Edge()
        mk = BRepBuilderAPI_MakeWire()
        for e in (e_in, s1, e_out, s0):
            mk.Add(e)
        if not mk.IsDone():
            return None
        strip = BRepBuilderAPI_MakeFace(surf, mk.Wire(), True).Face()
        BRepLib.BuildCurves3d_s(strip)
        if ms._area(strip) <= 0:
            return None
        fused = BRepAlgoAPI_Fuse(fwd, strip)
        fused.SetRunParallel(False)
        fused.Build()
        if not fused.IsDone():
            return None
        uni = ShapeUpgrade_UnifySameDomain(fused.Shape(), True, True, True)
        uni.Build()
        got = ms._faces(uni.Shape())
        if len(got) != 1:
            return None
        out = TopoDS.Face_s(got[0])
        if abs(ms._area(out) - ms._area(fwd) - ms._area(strip)) > 1e-4 * ms._area(fwd):
            return None
        return TopoDS.Face_s(out.Reversed()) if face.Orientation() == TopAbs_REVERSED else out
    except Exception as exc:  # noqa: BLE001 - not extended: the place is told
        logger.debug("surface mend: a trimmed face was not extended (%s: %s)", type(exc).__name__, exc)
        return None


def _corner(face, edge, at, out_dir: np.ndarray, reach_uv: float):
    """Where a strip's side ends: from the edge's end `at` (in the face's parameters), along the face's neighbouring
    edge there continued past it, as far as reaches `reach_uv` out from the edge; straight out (`out_dir`) when no
    neighbouring edge leaves that end at an angle."""
    from OCP.BRepAdaptor import BRepAdaptor_Curve2d
    from OCP.gp import gp_Pnt2d, gp_Vec2d
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad import midsurface as ms

    best = None
    em = ms._map(face, TopAbs_EDGE)
    for i in range(1, em.Extent() + 1):
        g = TopoDS.Edge_s(em.FindKey(i))
        if g.IsSame(edge):
            continue
        c = BRepAdaptor_Curve2d(g, face)
        for t, sgn in ((c.FirstParameter(), -1.0), (c.LastParameter(), 1.0)):
            if c.Value(t).Distance(at) > 1e-7 * max(1.0, abs(at.X()) + abs(at.Y())):
                continue
            p, v = gp_Pnt2d(), gp_Vec2d()
            c.D1(t, p, v)
            if v.Magnitude() < 1e-14:
                continue
            w = sgn * np.array([v.X(), v.Y()]) / v.Magnitude()      # on past the end, away from the edge's body
            along = float(np.dot(w, out_dir))
            if along > 0.2 and (best is None or along > best[0]):
                best = (along, w)
    w, k = (out_dir, 1.0) if best is None else (best[1], best[0])
    return gp_Pnt2d(at.X() + w[0] * reach_uv / k, at.Y() + w[1] * reach_uv / k)


#: SURF-12: a B-spline patch is rebuilt through this many points a side (more where it has more poles)
GRID_MIN = 17
#: ... and kept only when it lies on the old patch within this share of the sew tolerance
FIT_SHARE = 0.1


def _extended_bspline(bs, bounds, in_u: bool, after: bool, step: float, fit: float = 0.0):
    """A B-spline patch over `bounds`, continued past one side by `step` of its parameter (second-order: the
    surface's point, first and second derivative at the side), rebuilt through its points; None when the rebuilt
    patch strays from the old one."""
    from OCP.Approx import Approx_IsoParametric
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.GeomAPI import GeomAPI_PointsToBSplineSurface, GeomAPI_ProjectPointOnSurf
    from OCP.gp import gp_Pnt, gp_Vec
    from OCP.TColgp import TColgp_Array2OfPnt

    u0, u1, v0, v1 = bounds
    nu = max(GRID_MIN, 2 * bs.NbUPoles() + 1)
    nv = max(GRID_MIN, 2 * bs.NbVPoles() + 1)
    extra = 4                                            # rows past the side
    us = list(np.linspace(u0, u1, nu))
    vs = list(np.linspace(v0, v1, nv))

    def at(u, v):
        p = bs.Value(u, v)
        return np.array([p.X(), p.Y(), p.Z()])

    def beyond(u, v, s):
        """The point `s` of the parameter past the side at (u, v): p + s d + s^2/2 dd."""
        p, d1u, d1v, d2u, d2v, d2uv = gp_Pnt(), gp_Vec(), gp_Vec(), gp_Vec(), gp_Vec(), gp_Vec()
        bs.D2(u, v, p, d1u, d1v, d2u, d2v, d2uv)
        d1, d2 = (d1u, d2u) if in_u else (d1v, d2v)
        k = 1.0 if after else -1.0
        q = np.array([p.X(), p.Y(), p.Z()])
        return q + k * s * np.array([d1.X(), d1.Y(), d1.Z()]) + 0.5 * s * s * np.array([d2.X(), d2.Y(), d2.Z()])

    # the parameters past the side, and the grid (u index first, v second: the surface keeps its own orientation)
    out_pars = [step * (r + 1) / extra for r in range(extra)]
    if in_u:
        side_u = u1 if after else u0
        extra_rows = [[beyond(side_u, v, s) for v in vs] for s in out_pars]
        rows = [[at(u, v) for v in vs] for u in us]
        rows = rows + extra_rows if after else extra_rows[::-1] + rows
    else:
        side_v = v1 if after else v0
        rows = []
        for u in us:
            inside = [at(u, v) for v in vs]
            past = [beyond(u, side_v, s) for s in out_pars]
            rows.append(inside + past if after else past[::-1] + inside)
    n_a, n_b = len(rows), len(rows[0])
    arr = TColgp_Array2OfPnt(1, n_a, 1, n_b)
    for i in range(n_a):
        for j in range(n_b):
            arr.SetValue(i + 1, j + 1, gp_Pnt(*map(float, rows[i][j])))
    api = GeomAPI_PointsToBSplineSurface()
    api.Interpolate(arr, Approx_IsoParametric, False)
    new = api.Surface()
    # the rebuilt patch must lie on the old one where the old one was
    size = float(np.linalg.norm(at(u1, v1) - at(u0, v0))) or 1.0
    worst = 0.0
    for fu in (0.13, 0.5, 0.87):
        for fv in (0.11, 0.5, 0.89):
            q = at(u0 + fu * (u1 - u0), v0 + fv * (v1 - v0))
            pr = GeomAPI_ProjectPointOnSurf(gp_Pnt(*map(float, q)), new)
            if pr.NbPoints() == 0:
                return None
            worst = max(worst, float(pr.LowerDistance()))
    # within a tenth of the sew tolerance (`fit`); with none given, of a sew tolerance of the patch's own size - far
    # tighter on a small fillet patch than the part's own, and why those were never extended
    if worst > (fit if fit > 0 else FIT_SHARE * 1e-4 * size):
        return None
    return BRepBuilderAPI_MakeFace(new, 1e-7).Face()


def _keep_side(pieces: list, face, edge, tol: float = 0.0):
    """Of the pieces a face was split into, the one that holds the face's inside (a point well inside it): the nearest
    to that point, within `tol` - a B-spline continued past its edge is rebuilt through its own points, so it lies
    within the fit check (a tenth of the sew tolerance) of the old patch, not on it to the last digit (11 Nov: a bead
    patch continued down to its fillet found no piece of its own and was left open)."""
    from meshpipeline.cad import midsurface as ms

    q = ms._inner_point(face)
    if q is None:
        return None
    near = max(1e-6 * max(math.sqrt(ms._area(face)), 1.0), float(tol))
    scored = sorted(((ms._dist(ms._vertex(q), p), -ms._area(p), i) for i, p in enumerate(pieces)))
    return pieces[scored[0][2]] if scored and scored[0][0] <= near else None


def _split(face, tool) -> list:
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Splitter
    from OCP.TopTools import TopTools_ListOfShape

    from meshpipeline.cad import midsurface as ms

    args, tools = TopTools_ListOfShape(), TopTools_ListOfShape()
    args.Append(face)
    tools.Append(tool)
    sp = BRepAlgoAPI_Splitter()
    sp.SetArguments(args)
    sp.SetTools(tools)
    sp.SetRunParallel(False)
    sp.Build()
    return ms._faces(sp.Shape()) if sp.IsDone() else []


def _wall(edge, face, depth: float):
    """A cutting wall standing on `edge`, across `face` (the edge swept both ways along the face's normal)."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.gp import gp_Vec

    from meshpipeline.cad import surface_model as sm

    pts = sm._edge_points(edge, 3)
    n = _normal_at(face, pts[1])
    if n is None:
        return None
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Trsf

    t = gp_Trsf()
    t.SetTranslation(gp_Vec(*map(float, -n * depth)))
    start = BRepBuilderAPI_Transform(edge, t, True).Shape()
    return BRepPrimAPI_MakePrism(start, gp_Vec(*map(float, 2.0 * n * depth))).Shape()


def _try(sewn, k: int, new, old, band: float, nbr: int, tol_join: float, length_before: float):
    """A face replaced and the sheet sewn again: (the new Sewn, '') when face k's change is sound and the place closes
    (the two faces now share an edge there), else (None, why)."""
    from meshpipeline.cad import surface_clean as sc
    from meshpipeline.cad import surface_model as sm

    others = [f for j, f in enumerate(sewn.faces) if j not in (k, nbr)]
    why = _check(new, old, band, others, sewn.tol)
    if why:
        return None, why
    faces = list(sewn.faces)
    faces[k] = new
    pair = sm.Sewn(faces, list(sewn.names), sm._sheets(faces), sewn.tol, sewn.diag)
    rows = sc._couples(pair, tol_join)[1]
    good = [r for r in rows if not r["bad"] and set(r["on"]) == {k, nbr}]
    got = None
    if good:
        got, _rec = sc._one_at_a_time(pair, tol_join, good, {"places": [], "refused": ""}, sc._free(faces)[1])
    if got is None:
        # an edge now ending ON its neighbour's inside (a web's foot extended onto its plate): joined by scoring the
        # neighbour along it (SURF-2's imprint), as a rib's foot is
        got = sm.imprint(pair)
    if got is None:
        return None, "the changed face would still not meet its neighbour"
    return got, ""


def _close_one(here, p, e, k: int, nbr: int, gap: float, allowed: float, look: float):
    """SURF-12: one side of one place: face k's edge e, `gap` from face nbr. Moved, extended (and cut where it meets
    its neighbour) or trimmed back, the first that passes: (the new Sewn or None, why not, how, how far it moved)."""
    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad import surface_clean as sc
    from meshpipeline.cad import surface_model as sm

    a, b = here.faces[k], here.faces[nbr]
    length_before = sc._free(here.faces)[1]
    # the most a change may add or take away: a strip as wide as the reach along the edge
    band = 2.0 * allowed * ms._length(e) + 1e-6 * ms._area(a)
    fit = 0.1 * here.tol
    got, why, how, changed = None, "", "", gap
    mid = sm._edge_points(e, 3)[1]
    foot = _nearest(b, mid)
    na = _normal_at(a, mid)
    off = (abs(float(np.dot(foot - mid, na))) / max(float(np.linalg.norm(foot - mid)), 1e-300)
           if na is not None and float(np.linalg.norm(foot - mid)) > 0 else 0.0)
    in_plane = off < math.sin(math.radians(45.0))
    # 1. MOVED: the two faces sewn alone at the gap (a step, a corner off its neighbour's corner)
    rows = [] if in_plane else [r for r in sc._couples(
        sm.Sewn([a, b], ["a", "b"], [[0, 1]], here.tol, here.diag), 1.5 * gap)[1] if not r["bad"]]
    if rows:
        local = [dict(r, on=(k, nbr)) for r in rows]
        got, _rec = sc._one_at_a_time(here, 1.5 * gap, local, {"places": [], "refused": ""}, length_before)
        how = "moved onto its neighbour"
        if got is not None:
            for j in (k, nbr):
                w = _check(got.faces[j], here.faces[j], band, [f for i, f in enumerate(got.faces) if i not in (k, nbr)],
                           here.tol)
                if w:
                    got, why = None, w
                    break
        else:
            why = "the two faces would not sew at the gap"
    # 2. EXTENDED along its own surface, and cut where it meets its neighbour
    if got is None:
        ext = _extended(a, e, EXTEND_PAST * gap, fit)
        if ext is None:
            why = why or "the face could not be continued on its own surface past that edge"
        else:
            nb = _normal_at(b, mid)
            same = na is not None and nb is not None and abs(float(np.dot(na, nb))) > math.cos(math.radians(SAME_SIDE_DEG))
            if same:
                # the same sheet continued: past the gap, then cut back along the neighbour's own edge (so the new
                # edge lies on it, corners and all); a natural patch with no edge to cut along: up to the gap
                eb = [m[0] for m in p["_members"] if m[1] == nbr]
                wall = _wall(max(eb, key=ms._length), b, 10.0 * max(gap, here.tol)) if eb else None
                pieces = _split(ext, wall) if wall is not None else []
                new = _keep_side(pieces, a, e, here.tol) if len(pieces) > 1 else _extended(a, e, gap, fit)
            else:
                pieces = _split(ext, b)
                new = _keep_side(pieces, a, e, here.tol) if len(pieces) > 1 else None
                if new is None:
                    why = why or "the continued face does not reach its neighbour"
            if new is not None:
                got, w = _try(here, k, new, a, band, nbr, max(2.0 * here.tol, 0.25 * gap), length_before)
                why = w or why
                how = "extended along its own surface" + ("" if same else " and trimmed where it meets its neighbour")
                if got is not None:
                    changed = abs(ms._area(new) - ms._area(a)) / max(ms._length(e), 1e-300)
    # 3. TRIMMED back to the neighbour's edge (an overlap: the face runs over its neighbour)
    if got is None:
        eb = [m[0] for m in p["_members"] if m[1] == nbr]
        if eb:
            wall = _wall(max(eb, key=ms._length), b, 10.0 * max(gap, here.tol))
            pieces = _split(a, wall) if wall is not None else []
            new = _keep_side(pieces, a, e, here.tol) if len(pieces) > 1 else None
            if new is not None:
                changed = abs(ms._area(new) - ms._area(a)) / max(ms._length(e), 1e-300)
                if changed > allowed:
                    why = why or "the overlap is wider than it may be trimmed by"
                else:
                    got, w = _try(here, k, new, a, band, nbr, max(2.0 * here.tol, 1.5 * gap), length_before)
                    why = w or why
                    how = "trimmed back to its neighbour's edge"
    if got is None:
        return None, why or "no extension, trim or move closes it", "", gap
    # kept only when it leaves less open (fewer places, or as many with less open edge) and never more places
    bp = sm.open_places(here, mm_per_unit=1.0, reach=look)
    ap = sm.open_places(got, mm_per_unit=1.0, reach=look)
    bl = sum(float(x["length_mm"]) for x in bp)
    al = sum(float(x["length_mm"]) for x in ap)
    if len(ap) > len(bp) or (len(ap) == len(bp) and al >= bl - 2.0 * here.tol):
        return None, "the change would not leave less open", "", gap
    return got, "", how, changed


def mend(sewn, *, mm_per_unit: float, limit_mm: float | None = None,
         element_mm: float | None = None) -> tuple[Any, dict]:
    """SURF-12: the open places of a surface part's sewn sheets closed one at a time, nearest gap first: (the Sewn,
    the record in mm). `limit_mm`: the reach the user's notes give (None: REACH_PART of the part)."""
    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad import surface_model as sm

    mm = float(mm_per_unit)
    cap = REACH_PART * sewn.diag
    reach = float(limit_mm) / mm if limit_mm else cap
    done: list[dict] = []
    left: list[dict] = []
    tried: set = set()
    here = sewn
    look = max(reach, OPEN_REACH_LOOK * sewn.tol)
    for _round in range(400):
        places = [p for p in sm.open_places(here, mm_per_unit=1.0, reach=look) if tuple(map(tuple, p["pairs"])) not in tried]
        if not places:
            break
        places.sort(key=lambda p: (float(p["gap_mm"]), list(p["at_mm"])))
        p = places[0]
        tried.add(tuple(map(tuple, p["pairs"])))
        gap = float(p["gap_mm"])
        row = {"at_mm": [_r(c * mm) for c in p["at_mm"]], "gap_mm": _r(gap * mm)}
        # each side of the gap in turn: the face of the longest edge first, then its neighbour (a fillet's gap to the
        # main panel may be closed by changing either)
        sides: dict[int, tuple] = {}
        for m in sorted(p["_members"], key=lambda m: (-ms._length(m[0]), m[1])):
            sides.setdefault(m[1], m)
        got, how, changed, steps = None, "", gap, []
        for e, k, _g, nbr in list(sides.values())[:2]:
            a = here.faces[k]
            allowed = reach if limit_mm else min(reach, max(REACH_SELF * ms.width_of(a), float(element_mm or 0.0) / mm))
            row["faces"] = [here.names[k], here.names[nbr]]
            if gap > allowed:
                steps.append(f"{here.names[k]}: the gap is wider than the {allowed * mm:.2g} mm it may be changed by "
                             "(half its width, or the element size asked for, or 1/100 of the part)")
                continue
            got, why, how, changed = _close_one(here, p, e, k, nbr, gap, allowed, look)
            if got is not None:
                break
            steps.append(f"{here.names[k]}: {why}")
        if got is None:
            # the words say one plain reason (the first face's); the record keeps each face's
            row["why"] = steps[0].split(": ", 1)[1] if steps else "no extension, trim or move closes it"
            row["tried"] = steps
            left.append(row)
            continue
        before_n = len(sm.open_places(here, mm_per_unit=1.0, reach=look))
        after_n = len(sm.open_places(got, mm_per_unit=1.0, reach=look))
        here = got
        tried.discard(tuple(map(tuple, p["pairs"])))       # what is left of a place closed piece by piece is tried again
        row.update(how=how, face=row["faces"][0], moved_mm=_r(max(changed, gap) * mm), places_closed=before_n - after_n)
        done.append(row)
    record = {"reach_mm": _r(reach * mm), "reach_from": "notes" if limit_mm else "rule",
              "closed": int(sum(int(r["places_closed"]) for r in done)),
              "moved": sum(1 for r in done if r["how"].startswith("moved")),
              "extended": sum(1 for r in done if r["how"].startswith("extended")),
              "trimmed": sum(1 for r in done if r["how"].startswith("trimmed")),
              "widest_mm": _r(max([float(r["moved_mm"]) for r in done] + [0.0])),
              "changes": done[:40], "left": len(left), "left_places": left[:40]}
    return here, record


__all__ = ["EXTEND_PAST", "REACH_PART", "REACH_SELF", "mend"]
