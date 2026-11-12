# Responsibility: SURFACES MADE TO MEET (SURF-12): on a surface file's sheets (the surface route, no solid), each place
# where the surfaces do not meet (cad/surface_model.open_places: an edge running alongside another face, 0.4 to a few
# mm off) is closed the way a pre-processor user closes it by hand, the cheapest way that keeps the part's shape: the
# face EXTENDED along its own surface to its neighbour (an edge stopping short) or TRIMMED back to its neighbour's edge
# (an overlap); failing both, the edge MOVED onto its neighbour (a step or a corner off its neighbour's: the two faces
# sewn alone at the gap) - one place at a time, each change judged before it is kept and told: what was done, where,
# by how much. A tapered sliver between two faces that run on into each other is FILLED with a new face between their
# edges; a corner both sides stop short of has BOTH continued and cut where they cross (or the small side continued
# onto the large one's surface and the strip left there filled); a gap that is part of a HOLE closed round by its faces
# (a face the drawing left out) has the hole FILLED with one new face spanning it. What is still open after this is
# closed in the mesh (engines/surface_mesh_close, SURF-13).
# Boundaries: geometry only, deterministic, no model call. Only open sheets read by cad/surface_model (the surface
# route): solids and the extraction from solids are never touched, nor cad/repair (a closed skin's defects are CAD
# repair's). A gap is closed only when it is narrower than the reach (reach_of: the user's "close gaps up to 4 mm",
# else one element - the user's size, else the run's own - never past REACH_PART of the part); an extension continues
# the face's own surface, a trim cuts where the faces cross, and a result face must be valid, keep its normal side,
# keep its area within the band the change can explain, and cross no other face. Anything else is left to the mesh.
# Collaborates with: cad/surface_model.py (open places, the Sewn), cad/surface_clean.py (the pair sewing, the free
# edges), engines/surface_run.py (says what was done).
from __future__ import annotations

import logging
import math
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

#: SURF-12: a gap is closed when it is narrower than one element (the user's size, else the run's own) - but never
#: past this share of the part's size (1/100: the reach inside which an edge is an open place, SURF-8), unless the
#: user's notes allow more. (Until 12 Nov a change was also held to half the width of the face changed: a training
#: part's gaps beside 4 to 6 mm fillet strips stayed open for it.)
REACH_PART = 1e-2
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


def _check(new, old, band: float, others: list, tol: float, ends_on: list | None = None) -> str:
    """SURF-12: the result face judged: '' when it is sound, else why not. Valid; its normal side kept (at a point
    inside the old face); its area within `band` of the old; it crosses none of `others` (beyond touching), and the
    faces it was cut on (`ends_on`) only along its own rim."""
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
    for f in ends_on or []:
        sec = BRepAlgoAPI_Section(new, f)
        sec.Build()
        if sec.IsDone() and _crossing(sec.Shape(), new, f, tol, rims=(new,)):
            return "the changed face would cross another face"
    return ""


def _crossing(section, a, b, tol: float, rims: tuple | None = None) -> bool:
    """Whether two faces' section holds a curve that runs through the inside of either (a crossing), not only along
    the edges they share or touch at. `rims`: the faces whose rim a curve must lie on (default both: a curve on one
    face's rim across the other's inside is a T, which only the faces a continued face was cut on may have)."""
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
        on_rim = all(min(ms._dist(ms._vertex(mid), TopoDS.Edge_s(x)) for x in _edges(f)) <= 2.0 * tol
                     for f in (rims or (a, b)))
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


#: SURF-12: a strip continuing a face that cannot be continued on its own surface is built through this many points
#: along the edge
STRIP_POINTS = 33


def _strip(face, edge, length: float):
    """SURF-12: A STRIP CONTINUING `face` PAST `edge` by `length` when its own surface does not run on (a natural
    B-spline patch that does not extend well, a trimmed face whose surface ends at the edge): a ruled face from the
    edge, at every point straight on along the face's tangent plane, square to the edge and away from the face - the
    face continued tangent to itself, as a pre-processor's "extend edge" does. Its first side runs through the edge's
    own points, so it sews to the face at the sew tolerance. None when it cannot be built."""
    from OCP.Approx import Approx_IsoParametric
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.GeomAPI import GeomAPI_PointsToBSplineSurface
    from OCP.gp import gp_Pnt, gp_Vec
    from OCP.TColgp import TColgp_Array2OfPnt
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad import midsurface as ms

    if length <= 0:
        return None
    try:
        c = BRepAdaptor_Curve(TopoDS.Edge_s(edge))
        ts = np.linspace(c.FirstParameter(), c.LastParameter(), STRIP_POINTS)
        pts, outs = [], []
        for t in ts:
            p, v = gp_Pnt(), gp_Vec()
            c.D1(float(t), p, v)
            q = np.array([p.X(), p.Y(), p.Z()])
            n = _normal_at(face, q)
            tv = np.array([v.X(), v.Y(), v.Z()])
            if n is None or float(np.linalg.norm(tv)) < 1e-14:
                return None
            d = np.cross(n, tv)
            if float(np.linalg.norm(d)) < 1e-12:
                return None
            pts.append(q)
            outs.append(d / float(np.linalg.norm(d)))
        # away from the face: a step out from the edge's middle lands off it
        mid = len(pts) // 2
        eps = 0.25 * float(length)
        sign = 1.0 if ms._dist(ms._vertex(pts[mid] + eps * outs[mid]), face) > \
            ms._dist(ms._vertex(pts[mid] - eps * outs[mid]), face) else -1.0
        arr = TColgp_Array2OfPnt(1, STRIP_POINTS, 1, 3)
        for i, (q, d) in enumerate(zip(pts, outs, strict=True)):
            for j, s in enumerate((0.0, 0.5, 1.0)):
                arr.SetValue(i + 1, j + 1, gp_Pnt(*map(float, q + sign * s * float(length) * d)))
        api = GeomAPI_PointsToBSplineSurface()
        api.Interpolate(arr, Approx_IsoParametric, False)
        out = BRepBuilderAPI_MakeFace(api.Surface(), 1e-7).Face()
        # the face's own side up
        n0, n1 = _normal_at(face, pts[mid]), _normal_at(out, pts[mid] + sign * eps * outs[mid])
        if n0 is None or n1 is None:
            return None
        if float(np.dot(n0, n1)) < 0:
            out = TopoDS.Face_s(out.Reversed())
        return out
    except Exception as exc:  # noqa: BLE001 - no strip: the place is told
        logger.debug("surface mend: no strip (%s)", type(exc).__name__)
        return None


def _with_strip(here, p, e, k: int, nbr: int, gap: float, band: float, look: float):
    """SURF-12: face k continued past edge e by a tangent strip (_strip) cut where it meets its neighbour's sheet,
    added as a face of its own under face k's name, every face then fused (SURF-2's imprint) so the strip shares
    its edge with face k and its foot with the faces it ends on: (the new Sewn or None, why not, the strip's width)."""
    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad import surface_model as sm

    a = here.faces[k]
    strip = _strip(a, e, EXTEND_PAST * gap)
    if strip is None:
        return None, "the face could not be continued past that edge", gap
    touch = sorted(set(_cutters(here, k, strip)) | {nbr})
    pieces = _split(strip, ms._compound([here.faces[j] for j in touch]))
    if len(pieces) < 2:
        return None, "the continued face does not reach its neighbour", gap
    mid = sm._edge_points(e, 3)[1]
    near = sorted((ms._dist(ms._vertex(mid), f), i) for i, f in enumerate(pieces))
    piece = pieces[near[0][1]]
    if near[0][0] > 2.0 * here.tol:
        return None, "the continued face does not reach its neighbour", gap
    width = ms._area(piece) / max(ms._length(e), 1e-300)
    if width > EXTEND_PAST * gap + 2.0 * here.tol or ms._area(piece) > band:
        return None, "continuing the face would cover more than the gap", width
    others = [f for j, f in enumerate(here.faces) if j != k and j not in touch]
    why = _check_strip(piece, others, [here.faces[j] for j in touch], here.tol)
    if why:
        return None, why, width
    faces = list(here.faces) + [piece]
    got = _imprint_some(sm.Sewn(faces, list(here.names) + [here.names[k]], sm._sheets(faces), here.tol, here.diag),
                        sorted({k, len(faces) - 1} | set(touch) | set(_cutters(here, k, piece, own_too=True))))
    if got is None:
        return None, "the changed face would still not meet its neighbour", width
    return got, "", width


def _continued(face, edge, length: float, fit: float):
    """A face continued past `edge` by `length`: ("face", the whole face continued along its own surface) or, where
    its surface does not run on, ("strip", a tangent strip of its own, _strip); None when neither can be made."""
    ext = _extended(face, edge, length, fit)
    if ext is not None:
        return "face", ext
    strip = _strip(face, edge, length)
    return ("strip", strip) if strip is not None else None


def _cut_to(kind: str, shape, face, edge, tool, tol: float):
    """A continued face (or strip) cut where it meets `tool`: the piece holding the face (for a strip, the piece on
    the face's edge); None when the tool does not cut it."""
    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad import surface_model as sm

    pieces = _split(shape, tool)
    if len(pieces) < 2:
        return None
    if kind == "face":
        return _keep_side(pieces, face, edge, tol)
    mid = sm._edge_points(edge, 3)[1]
    near = sorted((ms._dist(ms._vertex(mid), f), i) for i, f in enumerate(pieces))
    return pieces[near[0][1]] if near[0][0] <= 2.0 * tol else None


def _with_both(here, p, gap: float, reach: float, fit: float):
    """SURF-12: A CORNER BOTH SIDES STOP SHORT OF (faces at an angle, each edge short of where the other's surface
    runs: one continued alone lands beside the other's edge, not on it): every face of the place continued past its
    edge there by EXTEND_PAST gaps (along its own surface, else by a tangent strip) and cut where it meets the other
    side's continuations - so both sides end on the one line where they cross, as an analyst extends two surfaces to
    meet and trims them there. (the new Sewn or None, why not, the most a face grew past its edge.)"""
    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad import surface_model as sm

    sides = _two_sides(p)
    if sides is None:
        return None, "three faces meet round the gap", gap
    length = EXTEND_PAST * gap
    edge_of: dict[int, Any] = {}
    for e, j, _g, _n in p["_members"]:
        if j not in edge_of or ms._length(e) > ms._length(edge_of[j]):
            edge_of[j] = e
    if not all(any(j in s for j in edge_of) for s in sides):
        # only one side's edges lie along the other (a small face beside a large face's long edge): that side alone
        # is continued, onto the other's surface, and the strip it then leaves in that surface is filled
        return _to_surface(here, p, sides, edge_of, gap, reach, fit)
    cont: dict[int, tuple] = {}
    for j in sorted(edge_of):
        c = _continued(here.faces[j], edge_of[j], length, fit)
        if c is None:
            return None, "the face could not be continued past that edge", gap
        cont[j] = c

    def tool(js) -> Any:
        return ms._compound([cont[j][1] if cont[j][0] == "face" else ms._compound([here.faces[j], cont[j][1]])
                             for j in sorted(js) if j in cont])

    new: dict[int, Any] = {}
    for s, other in (sides, sides[::-1]):
        t = tool(other)
        for j in sorted(s & set(cont)):
            got = _cut_to(cont[j][0], cont[j][1], here.faces[j], edge_of[j], t, here.tol)
            if got is None:
                return None, "the continued faces do not meet", gap
            new[j] = got
    width = 0.0
    for j, f in new.items():
        add = ms._area(f) - (ms._area(here.faces[j]) if cont[j][0] == "face" else 0.0)
        width = max(width, abs(add) / max(ms._length(edge_of[j]), 1e-300))
    if width > EXTEND_PAST * gap + 2.0 * here.tol:
        return None, "continuing the faces would cover more than the gap", width
    mine = set(new)
    others = [f for i, f in enumerate(here.faces) if i not in mine]
    for j, f in new.items():
        band = 2.0 * reach * ms._length(edge_of[j]) + 1e-6 * ms._area(here.faces[j])
        why = _check(f, here.faces[j], band, others, here.tol) if cont[j][0] == "face" else             _check_strip(f, others, [here.faces[i] for i in sorted(mine)], here.tol)
        if why:
            return None, why, width
    faces, names = list(here.faces), list(here.names)
    for j in sorted(new):
        if cont[j][0] == "face":
            faces[j] = new[j]
        else:
            faces.append(new[j])
            names.append(here.names[j])
    again = _resew_junctions(faces, here.tol)
    got = None if again is None else sm.Sewn(again, names, sm._sheets(again), here.tol, here.diag)
    if got is None:
        idx = sorted(mine | set(range(len(here.faces), len(faces))) |
                     {i for j in mine for i in _edge_neighbours(here, j)})
        got = _imprint_some(sm.Sewn(faces, names, sm._sheets(faces), here.tol, here.diag), idx)
    if got is None:
        return None, "the changed faces would still not meet", width
    return got, "", width


def _surface_around(face, reach: float):
    """The face's own surface over its parameter box widened by about `reach` mm on every side (a plane, cylinder,
    cone, sphere or torus run on; any other surface: the face itself)."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.BRepTools import BRepTools
    from OCP.Geom import (
        Geom_ConicalSurface,
        Geom_CylindricalSurface,
        Geom_Plane,
        Geom_RectangularTrimmedSurface,
        Geom_SphericalSurface,
        Geom_ToroidalSurface,
    )
    from OCP.gp import gp_Pnt, gp_Vec

    surf = BRep_Tool.Surface_s(face)
    while isinstance(surf, Geom_RectangularTrimmedSurface):
        surf = surf.BasisSurface()
    if not isinstance(surf, (Geom_Plane, Geom_CylindricalSurface, Geom_ConicalSurface, Geom_SphericalSurface,
                             Geom_ToroidalSurface)):
        return face
    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    p, du, dv = gp_Pnt(), gp_Vec(), gp_Vec()
    surf.D1(0.5 * (u0 + u1), 0.5 * (v0 + v1), p, du, dv)
    su, sv = max(du.Magnitude(), 1e-12), max(dv.Magnitude(), 1e-12)
    a, b, c, d = u0 - reach / su, u1 + reach / su, v0 - reach / sv, v1 + reach / sv
    if surf.IsUPeriodic() and b - a >= surf.UPeriod():
        a, b = u0, u1
    if surf.IsVPeriodic() and d - c >= surf.VPeriod():
        c, d = v0, v1
    try:
        return BRepBuilderAPI_MakeFace(surf, a, b, c, d, 1e-7).Face()
    except Exception:  # noqa: BLE001 - the face as it is
        return face


def _to_surface(here, p, sides, edge_of: dict, gap: float, reach: float, fit: float):
    """SURF-12, a corner one side stops short of (a small face beside a large face's long edge, at an angle): the
    small side's faces continued (_continued) and cut where they meet the large side's SURFACE run on past its edge
    (_surface_around); where the cut lands outside the large face - beside its edge, in its own surface - the strip
    left between that edge and the new one is filled (_fill_from). (the new Sewn or None, why not, the widest.)"""
    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad import surface_model as sm

    short, big = (sides[0], sides[1]) if any(j in sides[0] for j in edge_of) else (sides[1], sides[0])
    length = EXTEND_PAST * gap
    tool = ms._compound([_surface_around(here.faces[j], 2.0 * length) for j in sorted(big)])
    faces, names = list(here.faces), list(here.names)
    width = 0.0
    changed: list[int] = []
    for j in sorted(short):
        if j not in edge_of:
            continue
        c = _continued(here.faces[j], edge_of[j], length, fit)
        if c is None:
            return None, "the face could not be continued past that edge", gap
        new = _cut_to(c[0], c[1], here.faces[j], edge_of[j], tool, here.tol)
        if new is None:
            return None, "the continued face does not reach its neighbour", gap
        add = ms._area(new) - (ms._area(here.faces[j]) if c[0] == "face" else 0.0)
        width = max(width, abs(add) / max(ms._length(edge_of[j]), 1e-300))
        others = [f for i, f in enumerate(here.faces) if i != j and i not in big]
        band = 2.0 * reach * ms._length(edge_of[j]) + 1e-6 * ms._area(here.faces[j])
        why = _check(new, here.faces[j], band, others, here.tol) if c[0] == "face" else \
            _check_strip(new, others, [here.faces[i] for i in sorted(big | {j})], here.tol)
        if why:
            return None, why, width
        if c[0] == "face":
            faces[j] = new
            changed.append(j)
        else:
            faces.append(new)
            names.append(here.names[j])
            changed.append(len(faces) - 1)
    if width > EXTEND_PAST * gap + 2.0 * here.tol:
        return None, "continuing the face would cover more than the gap", width
    again = _resew_junctions(faces, here.tol)
    if again is None:
        return None, "the changed faces would still not meet", width
    mid = sm.Sewn(again, names, sm._sheets(again), here.tol, here.diag)
    # what the continued faces do not yet share with the large side: the strip in its surface, filled from its edge
    got, why, wid, _who = _fill_from(mid, set(big), set(changed), sorted(big | set(changed)), reach)
    if got is None:
        return None, why, width
    return got, "", max(width, wid)


def _check_strip(strip, others: list, ends_on: list, tol: float) -> str:
    """A strip judged as _check judges a changed face: valid; it crosses none of `others` and ends on `ends_on`
    only along its own rim."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Section
    from OCP.BRepCheck import BRepCheck_Analyzer

    from meshpipeline.cad import midsurface as ms

    if strip is None or strip.IsNull() or not BRepCheck_Analyzer(strip).IsValid():
        return "the changed face would not be valid"
    lo, hi = ms._box(strip)
    for f, rims in [(f, None) for f in others] + [(f, (strip,)) for f in ends_on]:
        flo, fhi = ms._box(f)
        if np.any(flo > hi + tol) or np.any(lo > fhi + tol):
            continue
        sec = BRepAlgoAPI_Section(strip, f)
        sec.Build()
        if sec.IsDone() and _crossing(sec.Shape(), strip, f, tol, rims=rims):
            return "the changed face would cross another face"
    return ""


#: SURF-12: A SLIVER FILLED. Two faces that run on into each other (their normals within SAME_SIDE_DEG) but leave a
#: strip between their free edges - tapering from nil at one end to most of an element at the other, the strip in
#: both faces' tangent planes (within FILL_PLANE_DEG) - get a new face spanning the strip, as an analyst fills it
#: between the two edges: ruled from the one side's edge (through FILL_POINTS of its own points, so it sews to it at
#: the sew tolerance) straight across to the nearest points of the other side's free edges. The faces themselves are
#: never moved. The way across must look across the strip (within FILL_ACROSS_DEG of square to the edge): an edge
#: running past the end of the other side is filled only where it faces it.
FILL_POINTS = 25
FILL_PLANE_DEG = 15.0
FILL_ACROSS_DEG = 45.0
#: ... and a new face ends where the strip narrows to this share of the sew tolerance: the rest, to where the two
#: edges touch, is sewn as it lies (a face of no width at its end would not be valid)
FILL_END = 0.1


def _two_sides(p) -> tuple[set[int], set[int]] | None:
    """A place's faces in two sides, each pair's two faces on opposite sides: (the side the new faces are ruled from,
    the other). The first is the side more of whose edges were found lying along the other (the longer, by length;
    ties the side holding the lowest face): its edges face the strip. None when the pairs do not split so (three
    faces round one gap)."""
    from meshpipeline.cad import midsurface as ms

    adj: dict[int, set[int]] = {}
    for a, b in p["pairs"]:
        adj.setdefault(int(a), set()).add(int(b))
        adj.setdefault(int(b), set()).add(int(a))
    side: dict[int, int] = {}
    for s in sorted(adj):
        if s in side:
            continue
        side[s] = 0
        stack = [s]
        while stack:
            u = stack.pop()
            for v in sorted(adj[u]):
                if v not in side:
                    side[v] = 1 - side[u]
                    stack.append(v)
                elif side[v] == side[u]:
                    return None
    s0 = {k for k, c in side.items() if c == 0}
    s1 = {k for k, c in side.items() if c == 1}
    l0 = sum(ms._length(m[0]) for m in p["_members"] if m[1] in s0)
    l1 = sum(ms._length(m[0]) for m in p["_members"] if m[1] in s1)
    return (s0, s1) if (-l0, min(s0, default=0)) <= (-l1, min(s1, default=0)) else (s1, s0)


def _fill_runs(e, face, across, tol: float, reach: float) -> tuple[list[tuple[list, list]], float]:
    """A free edge e of `face` against the other side's free edges `across` (a compound): the stretches of e that
    look across a strip at them - each point within `reach` of them, wider than the sew tolerance, the way across
    square to the edge (within FILL_ACROSS_DEG) and in the face's own tangent plane (within FILL_PLANE_DEG) - each
    as (its points, their nearest points across); and the strip's widest. A stretch ends where the strip narrows to
    FILL_END of the sew tolerance (found between the samples, by halves) or stops facing."""
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.gp import gp_Pnt, gp_Vec

    from meshpipeline.cad import midsurface as ms

    c = BRepAdaptor_Curve(e)
    t0, t1 = c.FirstParameter(), c.LastParameter()

    def at(t: float):
        p, v = gp_Pnt(), gp_Vec()
        c.D1(float(t), p, v)
        q = np.array([p.X(), p.Y(), p.Z()])
        d = BRepExtrema_DistShapeShape(ms._vertex(q), across)
        if not d.IsDone() or not d.NbSolution():
            return q, None, np.inf, np.array([v.X(), v.Y(), v.Z()])
        w = d.PointOnShape2(1)
        return q, np.array([w.X(), w.Y(), w.Z()]), float(d.Value()), np.array([v.X(), v.Y(), v.Z()])

    def kind(r) -> int:
        """1: faces across a strip; 0: touching (within the sew tolerance); -1: no strip there."""
        q, f, dist, tv = r
        if f is None or dist > reach:
            return -1
        if dist <= 1.5 * tol:
            return 0
        way = (f - q) / dist
        tl = float(np.linalg.norm(tv))
        if tl > 1e-14 and abs(float(np.dot(way, tv))) / tl > math.cos(math.radians(90.0 - FILL_ACROSS_DEG)):
            return -1
        n = _normal_at(face, q)
        if n is None or abs(float(np.dot(way, n))) > math.sin(math.radians(FILL_PLANE_DEG)):
            return -1
        return 1

    ts = np.linspace(t0, t1, 4 * FILL_POINTS + 1)
    rows = [at(t) for t in ts]
    kinds = [kind(r) for r in rows]
    widest = max((r[2] for r, k in zip(rows, kinds, strict=True) if k == 1), default=0.0)
    # a stretch: samples facing or touching, the strip wider than FILL_END of the tolerance, at least one facing
    end = FILL_END * tol
    ok = [k >= 0 and r[2] > end for k, r in zip(kinds, rows, strict=True)]
    runs: list[tuple[float, float]] = []
    i = 0
    while i < len(ts):
        if not ok[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(ts) and ok[j + 1]:
            j += 1
        lo, hi = float(ts[i]), float(ts[j])
        # an end next to a sample where the edges touch moves on to where the strip narrows to FILL_END (to the
        # edge's own end when they touch there), and the new face closes to a point there
        if i > 0 and kinds[i - 1] == 0:
            a, b = float(ts[i - 1]), float(ts[i])
            if i - 1 > 0:
                for _ in range(24):
                    m = 0.5 * (a + b)
                    a, b = (m, b) if at(m)[2] <= end else (a, m)
            lo = a
        if j + 1 < len(ts) and kinds[j + 1] == 0:
            a, b = float(ts[j]), float(ts[j + 1])
            if j + 1 < len(ts) - 1:
                for _ in range(24):
                    m = 0.5 * (a + b)
                    a, b = (a, m) if at(m)[2] <= end else (m, b)
            hi = b
        if j > i and any(k == 1 for k in kinds[i:j + 1]):
            runs.append((lo, hi))
        i = j + 1
    out = []
    for lo, hi in runs:
        got = [at(t) for t in np.linspace(lo, hi, FILL_POINTS)]
        if any(g[1] is None for g in got):
            continue
        out.append(([g[0] for g in got], [g[0] if g[2] <= end else g[1] for g in got]))
    return out, widest


def _fill_face(face, pts: list, feet: list):
    """A ruled face from the points `pts` (on face's edge) straight across to `feet`, face's own side up."""
    from OCP.Approx import Approx_IsoParametric
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.GeomAPI import GeomAPI_PointsToBSplineSurface
    from OCP.gp import gp_Pnt
    from OCP.TColgp import TColgp_Array2OfPnt
    from OCP.TopoDS import TopoDS

    n = len(pts)
    arr = TColgp_Array2OfPnt(1, n, 1, 2)
    for i, (q, f) in enumerate(zip(pts, feet, strict=True)):
        arr.SetValue(i + 1, 1, gp_Pnt(*map(float, q)))
        arr.SetValue(i + 1, 2, gp_Pnt(*map(float, f)))
    api = GeomAPI_PointsToBSplineSurface()
    api.Interpolate(arr, Approx_IsoParametric, False)
    out = BRepBuilderAPI_MakeFace(api.Surface(), 1e-7).Face()
    mid = n // 2
    c = 0.5 * (np.asarray(pts[mid]) + np.asarray(feet[mid]))
    n0, n1 = _normal_at(face, pts[mid]), _normal_at(out, c)
    if n0 is not None and n1 is not None and float(np.dot(n0, n1)) < 0:
        out = TopoDS.Face_s(out.Reversed())
    return out


def _free_edges_of(faces: list) -> list[list]:
    """Each face's free edges (held by that face alone among `faces`)."""
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    from meshpipeline.cad import midsurface as ms

    owners = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(ms._compound(faces), TopAbs_EDGE, TopAbs_FACE, owners)
    out = []
    for f in faces:
        mine = []
        for e in _edges(f):
            i = owners.FindIndex(e)
            if i > 0 and owners.FindFromIndex(i).Extent() == 1:
                mine.append(TopoDS.Edge_s(e))
        out.append(mine)
    return out


def _with_fill(here, p, reach: float, look: float):
    """SURF-12: the strip of place p between two sides that run on into each other filled (FILL_POINTS): new faces
    ruled from the first side's free edges (_two_sides) across to the other side's, each under its own side's face
    name, then sewn in. (the new Sewn or None, why not, the strip's widest, (face, face beside) to name in the
    words)."""
    sides = _two_sides(p)
    if sides is None:
        return None, "three faces meet round the gap", 0.0, None
    pairs = {(int(a), int(b)) for a, b in p["pairs"]}
    return _fill_from(here, sides[0], sides[1], sorted({k for pr in pairs for k in pr}), reach)


def _fill_from(here, first: set[int], second: set[int], touch: list[int], reach: float):
    """SURF-12: new faces ruled from the free edges of the faces `first` across a strip to those of the faces
    `second` (_fill_runs), each under its own side's face name, checked (crossing no face but `touch` - and those only
    along its own rim) and sewn in. (the new Sewn or None, why not, the strip's widest, (face, face beside).)"""
    from OCP.BRepCheck import BRepCheck_Analyzer

    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad import surface_model as sm

    free = _free_edges_of(here.faces)
    new: list = []
    named: list[tuple[int, int]] = []
    widest = 0.0
    # the other side's free edges, all of them: a face's edge may run on past the faces it was paired with
    beside = sorted(second)
    opp = [e for j in beside for e in free[j]]
    if not opp:
        return None, "the faces' edges do not face each other across a strip", 0.0, None
    across = ms._compound(opp)
    for k in sorted(first):
        for e in free[k]:
            runs, wid = _fill_runs(e, here.faces[k], across, here.tol, reach)
            if not runs:
                continue
            widest = max(widest, wid)
            for pts, feet in runs:
                try:
                    f = _fill_face(here.faces[k], pts, feet)
                except Exception as exc:  # noqa: BLE001 - not filled: the place is told
                    logger.debug("surface mend: no fill (%s)", type(exc).__name__)
                    return None, "the new face would not be valid", widest, None
                if f is None or f.IsNull() or not BRepCheck_Analyzer(f).IsValid():
                    return None, "the new face would not be valid", widest, None
                mid = pts[len(pts) // 2]
                named.append((k, min(beside, key=lambda j: ms._dist(ms._vertex(mid), here.faces[j]))))
                new.append(f)
    if not new:
        return None, "the faces' edges do not face each other across a strip", widest, None
    # each new face crosses no face (it may end on the faces it fills between, along its own rim only)
    others = [f for j, f in enumerate(here.faces) if j not in touch]
    for f in new:
        w = _check_strip(f, others, [here.faces[j] for j in touch], here.tol)
        if w:
            return None, w, widest, None
    faces = list(here.faces) + new
    names = list(here.names) + [here.names[k] for k, _b in named]
    # joined by the local fuse (the sew tolerance as its fuzzy value): the faces beside are cut where a new face's
    # corner lands along their edges, so each new edge is shared - then sewn again with junctions kept
    again = _resew_junctions(faces, here.tol)
    got = None if again is None else sm.Sewn(again, names, sm._sheets(again), here.tol, here.diag)
    if got is None:
        got = _imprint_some(sm.Sewn(faces, names, sm._sheets(faces), here.tol, here.diag),
                            sorted(set(touch) | set(range(len(here.faces), len(faces)))))
    if got is None:
        return None, "the new face would not sew to the faces beside it", widest, None
    return got, "", widest, named[0]


#: SURF-12: A HOLE FILLED. A place's edge may lie on the rim of a HOLE - free edges closing into a loop round a face
#: the drawing left out (the tapering end of a strip in a bend, a corner patch between three fillets) - where no one
#: edge faces another across a strip, so neither a face continued nor a strip filled closes it. When the hole is no
#: more than HOLE_REACHES reaches across (the widest circle inside it), it gets one new face spanning the loop -
#: OpenCASCADE's n-sided filling, tangent to the faces round it where they allow, else through the rim alone - as an
#: analyst fills a hole from its edges. The faces round it are never moved.
HOLE_REACHES = 2.0
#: ... its width read on a grid of this many points a side inside the new face
HOLE_GRID = 13
#: ... and a loop longer than this many reaches is the sheet's own outline, or an opening, not a missing face
HOLE_ROUND = 60.0
#: ... and a new face that leaves a face's other free edge beside it (a sliver between two holes) has the hole there
#: filled in the same change, up to this many more
HOLE_CHAIN = 2


def _hole_loop(here, edge, longest: float) -> list[tuple[Any, int]] | None:
    """SURF-12: the hole the free edge `edge` lies on the rim of: the shortest loop of free edges through it, corners
    within the sew tolerance of each other taken as one (two faces' corners meeting unsewn) - as [(edge, its face)],
    in order round the loop. None when the edge closes no loop no longer than `longest`."""
    import heapq

    from OCP.BRep import BRep_Tool
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    from meshpipeline.cad import midsurface as ms

    free = _free_edges_of(here.faces)
    vm = TopTools_IndexedMapOfShape()
    rows: list[tuple[Any, int, int, int, float]] = []
    for k, es in enumerate(free):
        for e in es:
            a, b = vm.Add(TopExp.FirstVertex_s(e)), vm.Add(TopExp.LastVertex_s(e))
            rows.append((e, k, a, b, ms._length(e)))
    start = next((i for i, r in enumerate(rows) if r[0].IsSame(edge)), None)
    if start is None:
        return None
    pts = np.array([(lambda q: (q.X(), q.Y(), q.Z()))(BRep_Tool.Pnt_s(TopoDS.Vertex_s(vm.FindKey(i))))
                    for i in range(1, vm.Extent() + 1)]).reshape(-1, 3)
    one = list(range(len(pts) + 1))
    for i in range(1, len(pts)):
        near = np.where(np.linalg.norm(pts[:i] - pts[i], axis=1) <= here.tol)[0]
        if len(near):
            one[i + 1] = one[int(near[0]) + 1]
    rows = [(e, k, one[a], one[b], w) for e, k, a, b, w in rows]
    _e, _k, s, t, w0 = rows[start]
    if s == t:
        return [(rows[start][0], rows[start][1])] if w0 <= longest else None
    nbr: dict[int, list[tuple[int, int, float]]] = {}
    for i, (_e, _k, a, b, w) in enumerate(rows):
        if i != start and a != b:
            nbr.setdefault(a, []).append((b, i, w))
            nbr.setdefault(b, []).append((a, i, w))
    best: dict[int, float] = {t: w0}
    prev: dict[int, tuple[int, int]] = {}
    heap = [(w0, t)]
    while heap:
        d, u = heapq.heappop(heap)
        if u == s or d > longest:
            break
        if d > best.get(u, math.inf):
            continue
        for v, i, w in nbr.get(u, []):
            if d + w < best.get(v, math.inf) - 1e-12:
                best[v], prev[v] = d + w, (u, i)
                heapq.heappush(heap, (d + w, v))
    if s not in prev or best[s] > longest:
        return None
    path, u = [start], s
    while u != t:
        u, i = prev[u]
        path.append(i)
    return [(rows[i][0], rows[i][1]) for i in path]


def _hole_face(here, loop: list[tuple[Any, int]]):
    """SURF-12: one new face spanning the hole `loop`: n-sided filling tangent to the faces round it (its tangency kept
    within SAME_SIDE_DEG), else through its rim alone; mended to a valid face where the filling leaves its outline a
    little off (a corner of no angle at a tapering end); on the rim within a tenth of the sew tolerance, so it sews to
    the faces round it. Its side up as the face of the loop's longest edge. None when no such face can be made."""
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepOffsetAPI import BRepOffsetAPI_MakeFilling
    from OCP.GeomAbs import GeomAbs_C0, GeomAbs_G1
    from OCP.ShapeFix import ShapeFix_Shape
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad import midsurface as ms

    for tangent in (True, False):
        try:
            mf = BRepOffsetAPI_MakeFilling()
            for e, k in loop:
                if tangent:
                    mf.Add(e, here.faces[k], GeomAbs_G1)
                else:
                    mf.Add(e, GeomAbs_C0)
            mf.Build()
            if not mf.IsDone() or mf.G0Error() > FIT_SHARE * here.tol or (
                    tangent and mf.G1Error() > math.radians(SAME_SIDE_DEG)):
                continue
            got = ms._faces(mf.Shape())
            if len(got) != 1:
                continue
            f = TopoDS.Face_s(got[0])
            if not BRepCheck_Analyzer(f).IsValid():
                fix = ShapeFix_Shape(f)
                fix.Perform()
                got = ms._faces(fix.Shape())
                if len(got) != 1 or not BRepCheck_Analyzer(got[0]).IsValid():
                    continue
                f = TopoDS.Face_s(got[0])
            e, k = max(loop, key=lambda r: ms._length(r[0]))
            q = ms._inner_point(f)
            if q is None:
                continue
            n0, n1 = _normal_at(here.faces[k], _nearest(here.faces[k], q)), _normal_at(f, q)
            if n0 is not None and n1 is not None and float(np.dot(n0, n1)) < 0:
                f = TopoDS.Face_s(f.Reversed())
            return f
        except Exception as exc:  # noqa: BLE001 - not filled: the place is told
            logger.debug("surface mend: no hole face (%s)", type(exc).__name__)
    return None


def _across_inside(face, rim) -> float:
    """How wide a face is across: twice the farthest any point inside it (on a HOLE_GRID grid of its parameters) lies
    from `rim` (its outline's edges)."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepClass import BRepClass_FaceClassifier
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Pnt2d
    from OCP.TopAbs import TopAbs_IN

    from meshpipeline.cad import midsurface as ms

    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    surf = BRepAdaptor_Surface(face)
    far = 0.0
    for u in np.linspace(u0, u1, HOLE_GRID + 2)[1:-1]:
        for v in np.linspace(v0, v1, HOLE_GRID + 2)[1:-1]:
            if BRepClass_FaceClassifier(face, gp_Pnt2d(float(u), float(v)), 1e-7).State() != TopAbs_IN:
                continue
            q = surf.Value(float(u), float(v))
            d = BRepExtrema_DistShapeShape(ms._vertex(np.array([q.X(), q.Y(), q.Z()])), rim)
            if d.IsDone() and d.NbSolution():
                far = max(far, float(d.Value()))
    return 2.0 * far


def _with_hole(here, p, reach: float):
    """SURF-12: the holes place p's edges lie on the rims of (one, or one each side of a sliver the place runs past)
    each filled with a new face (HOLE_REACHES): (the new Sewn or None, why not, how wide the widest hole is across,
    the names of the faces round them)."""
    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad import surface_model as sm

    loops: list[tuple[list, int]] = []
    for e, k, _g, _n in sorted(p["_members"], key=lambda m: (-ms._length(m[0]), m[1])):
        if any(any(e.IsSame(x) for x, _j in lp) for lp, _k in loops):
            continue
        loop = _hole_loop(here, e, HOLE_ROUND * reach)
        if loop is not None:
            loops.append((loop, k))
    if not loops:
        return None, "the gap is not part of a hole its faces close round", 0.0, []
    round_ = sorted({k for lp, _k in loops for _e, k in lp})
    names = sorted({here.names[k] for k in round_}, key=lambda n: (len(n), n))
    new, labels, wide = [], [], 0.0
    others = [f for j, f in enumerate(here.faces) if j not in round_]
    for loop, k0 in loops:
        face = _hole_face(here, loop)
        if face is None:
            return None, "the hole round the gap could not be filled with a valid face", wide, names
        wide = max(wide, _across_inside(face, ms._compound([e for e, _k in loop])))
        if wide > HOLE_REACHES * reach:
            return None, "the hole round the gap is more than twice as wide as the gaps closed here", wide, names
        why = _check_strip(face, others, [here.faces[j] for j in round_], here.tol)
        if why:
            return None, why, wide, names
        new.append(face)
        labels.append(here.names[k0])
    faces = list(here.faces) + new
    labels = list(here.names) + labels
    again = _resew_junctions(faces, here.tol)
    got = None if again is None else sm.Sewn(again, labels, sm._sheets(again), here.tol, here.diag)
    if got is None:
        got = _imprint_some(sm.Sewn(faces, labels, sm._sheets(faces), here.tol, here.diag),
                            sorted(set(round_) | set(range(len(here.faces), len(faces)))))
    if got is None:
        return None, "the new face would not sew to the faces round it", wide, names
    return got, "", wide, names


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


def _cutters(sewn, k: int, ext, own_too: bool = False) -> list[int]:
    """The faces a continued face `ext` (face k's) reaches: every face but k and the faces sharing an edge with k (its
    own sheet's neighbours run on beside it, they are not what it should meet) whose box comes within the sew
    tolerance of ext's and that ext's section with crosses or touches. `own_too`: k's neighbours as well."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Section
    from OCP.TopAbs import TopAbs_EDGE

    from meshpipeline.cad import midsurface as ms

    own = ms._map(sewn.faces[k], TopAbs_EDGE)
    lo, hi = ms._box(ext)
    out = []
    for j, f in enumerate(sewn.faces):
        if j == k:
            continue
        flo, fhi = ms._box(f)
        if np.any(flo > hi + sewn.tol) or np.any(lo > fhi + sewn.tol):
            continue
        if not own_too and any(own.Contains(x) for x in _edges(f)):
            continue
        sec = BRepAlgoAPI_Section(ext, f)
        sec.Build()
        if sec.IsDone() and ms._map(sec.Shape(), TopAbs_EDGE).Extent():
            out.append(j)
    return out


def _edge_neighbours(sewn, k: int) -> list[int]:
    """The faces sharing an edge with face k."""
    from OCP.TopAbs import TopAbs_EDGE

    from meshpipeline.cad import midsurface as ms

    own = ms._map(sewn.faces[k], TopAbs_EDGE)
    return [j for j, f in enumerate(sewn.faces) if j != k and any(own.Contains(x) for x in _edges(f))]


def _resew_junctions(faces: list, tol: float) -> list | None:
    """The faces sewn at `tol` with junctions kept (the sewing's non-manifold mode: an edge three faces share stays
    one), each in its place; None when the sewing loses or merges a face."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    from meshpipeline.cad import midsurface as ms

    sewing = BRepBuilderAPI_Sewing(float(tol), True, True, True, True)
    sewing.Add(ms._compound(faces))
    sewing.Perform()
    result = sewing.SewedShape()
    if result is None or result.IsNull() or int(sewing.NbDeletedFaces()) > 0:
        return None
    index = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(result, TopAbs_FACE, index)
    out, seen = [], set()
    for f in faces:
        g = sewing.ModifiedSubShape(f)
        k = index.FindIndex(g) if g is not None and not g.IsNull() else 0
        if k <= 0 or k in seen:
            return None
        seen.add(k)
        out.append(TopoDS.Face_s(index.FindKey(k)))
    return out


def _imprint_some(sewn, idx: list[int]):
    """SURF-2's imprint on a few faces only (`idx`: the face changed and those it ends on): they are fused (the sew
    tolerance as the fuzzy value) so each that another's edge lies on is scored along it; every other face is kept
    as it is (the fuse leaves an edge it does not cut as it was, so the faces beside still share it). Each piece keeps
    its face's name. None when the fuse fails, loses a face or changes the area."""
    from OCP.BOPAlgo import BOPAlgo_Builder
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad import surface_model as sm

    try:
        bop = BOPAlgo_Builder()
        for j in idx:
            bop.AddArgument(sewn.faces[j])
        bop.SetRunParallel(False)
        bop.SetNonDestructive(True)
        bop.SetFuzzyValue(float(sewn.tol))
        bop.Perform()
        if bop.HasErrors():
            return None
        faces, names = [], []
        chosen = set(idx)
        after = 0.0
        for j, (f, name) in enumerate(zip(sewn.faces, sewn.names, strict=True)):
            if j not in chosen:
                faces.append(f)
                names.append(name)
                continue
            images = [TopoDS.Face_s(x) for x in bop.Modified(f)] or ([] if bop.IsDeleted(f) else [f])
            if not images:
                return None
            faces += images
            names += [name] * len(images)
            after += sum(ms._area(g) for g in images)
        before = sum(ms._area(sewn.faces[j]) for j in chosen)
        if abs(after - before) > sm.IMPRINT_AREA * max(before, 1e-300) + 1e-9:
            return None
        # the fuse rebuilds the edges of the faces it cuts: those they shared with a face outside it are sewn back at
        # the sew tolerance (only what already coincides is joined; a junction stays one edge)
        again = _resew_junctions(faces, sewn.tol)
        if again is None:
            return None
        return sm.Sewn(again, names, sm._sheets(again), sewn.tol, sewn.diag)
    except Exception as exc:  # noqa: BLE001 - not joined: the place is told
        logger.debug("surface mend: no local imprint (%s)", type(exc).__name__)
        return None


def _try(sewn, k: int, new, old, band: float, nbr: int, tol_join: float, length_before: float,
         touch: list[int] | None = None):
    """A face replaced and the sheet sewn again: (the new Sewn, '') when face k's change is sound and the place closes
    (the two faces now share an edge there), else (None, why). `touch`: the faces the new face was cut on - it may end
    on them (its new edge across their inside), not cross them."""
    from meshpipeline.cad import surface_clean as sc
    from meshpipeline.cad import surface_model as sm

    ends_on = set(touch or [])
    others = [f for j, f in enumerate(sewn.faces) if j not in (k, nbr) and j not in ends_on]
    why = _check(new, old, band, others, sewn.tol, ends_on=[sewn.faces[j] for j in sorted(ends_on - {nbr})])
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
        # neighbour along it (SURF-2's imprint), as a rib's foot is - only the faces concerned fused: the changed
        # face, its own neighbours (so it shares its sides with them again) and those it ends on (12 Nov: the whole
        # part fused took minutes a try on a 124-patch look-alike); the whole part when that fails
        # (and every face the new one touches along an edge: a neighbour continued before it runs on beside it)
        got = _imprint_some(pair, sorted({k, nbr} | ends_on | set(_edge_neighbours(sewn, k)) |
                                         set(_cutters(sewn, k, new, own_too=True))))
    if got is None:
        got = sm.imprint(pair)
    if got is None:
        return None, "the changed face would still not meet its neighbour"
    return got, ""


def _close_one(here, p, e, k: int, nbr: int, gap: float, allowed: float, look: float,
               last: bool = False, said: dict | None = None):
    """SURF-12: one side of one place: face k's edge e, `gap` from face nbr. Extended (and cut where it meets its
    neighbour), trimmed back, or moved, the first that passes: (the new Sewn or None, why not, how, how far it
    moved). `said`: filled with what the words need beyond that (a hole's faces round it, "round")."""
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
    nb0 = _normal_at(b, mid)
    # a STEP: the neighbour faces the same way but lies off the face's own surface - continuing or cutting the face
    # along its own surface only slides it under or over its neighbour (12 Nov: a strip lifted 2.4 mm had its
    # neighbour continued 2.5 mm underneath it); its edge is moved instead
    step = not in_plane and na is not None and nb0 is not None and \
        abs(float(np.dot(na, nb0))) > math.cos(math.radians(SAME_SIDE_DEG))
    # a change is kept only when it leaves less open (fewer places, or as many with less open edge) and never more
    # places; one that does not gives way to the next kind
    bp = sm.open_places(here, mm_per_unit=1.0, reach=look)
    bl = sum(float(x["length_mm"]) for x in bp)

    def less_open(new_sewn) -> bool:
        ap = sm.open_places(new_sewn, mm_per_unit=1.0, reach=look)
        al = sum(float(x["length_mm"]) for x in ap)
        return not (len(ap) > len(bp) or (len(ap) == len(bp) and al >= bl - 2.0 * here.tol))

    def gone(new_sewn) -> bool:
        """The place closed whole: fewer places after (a last way that closes part of a place leaves the rest
        harder for the mesh to close than the whole was - 12 Nov: a 1.5 mm place three faces round, which the mesh
        closed, filled along one face only, left 0.75 mm the mesh could not)."""
        ap = sm.open_places(new_sewn, mm_per_unit=1.0, reach=look)
        if len(ap) >= len(bp):
            return False
        # ... and no place left that was not there before (by its faces): a change that moves the gap elsewhere
        # among the same faces has not closed it (the same 1.5 mm place, filled along one face, left a new 0.75 mm
        # place along another the mesh then could not close)
        before = {tuple(map(tuple, x["pairs"])) for x in bp}
        return all(tuple(map(tuple, x["pairs"])) in before for x in ap)

    if last:
        # THE LAST WAYS, once both faces' own continuations, trims and moves have failed (a new face, or both
        # faces changed): 1d and 1c
        # 1d. BOTH CONTINUED: a corner both faces stop short of - each continued alone lands beside the other's edge
        if not step and na is not None and nb0 is not None and \
                abs(float(np.dot(na, nb0))) <= math.cos(math.radians(SAME_SIDE_DEG)):
            bgot, _bwhy, width = _with_both(here, p, gap, allowed, fit)
            if bgot is not None and gone(bgot):
                return bgot, "", "extended both to meet each other and trimmed where they cross", width
        # 1c. FILLED: two faces running on into each other (a fillet and the panel beside it) leave a strip between their
        # free edges, tapering along the edge - a new face spans it (_with_fill); neither face is moved
        if in_plane and not step and na is not None and nb0 is not None and \
                abs(float(np.dot(na, nb0))) > math.cos(math.radians(SAME_SIDE_DEG)):
            fgot, fwhy, width, _who = _with_fill(here, p, allowed, look)
            if fgot is not None and not gone(fgot):
                fgot, fwhy = None, "the new face would close only part of the gap"
            if fgot is not None:
                return fgot, "", "filled with a new face", width
            why = fwhy or why
        # 1e. A HOLE FILLED: the gap is part of a hole its faces close round (a face the drawing left out) - one new
        # face spans it
        hgot, hwhy, width, round_ = _with_hole(here, p, allowed)
        before = {tuple(map(tuple, x["pairs"])) for x in bp}
        for _more in range(HOLE_CHAIN):
            # a face between two holes (a sliver both its free edges are on): the first hole filled leaves its
            # other edge beside the new face - the hole there is filled in the same change
            if hgot is None or gone(hgot):
                break
            fresh = [x for x in sm.open_places(hgot, mm_per_unit=1.0, reach=look)
                     if tuple(map(tuple, x["pairs"])) not in before and float(x["gap_mm"]) <= allowed]
            if not fresh:
                break
            nxt = min(fresh, key=lambda x: (float(x["gap_mm"]), list(x["at_mm"])))
            ngot, _nwhy, nwidth, nround = _with_hole(hgot, nxt, allowed)
            if ngot is None:
                break
            hgot, width = ngot, max(width, nwidth)
            round_ = sorted(set(round_) | set(nround), key=lambda n: (len(n), n))
        if hgot is not None and not gone(hgot):
            hgot, hwhy = None, "the new face would close only part of the gap"
        if hgot is not None:
            if said is not None:
                said["round"] = round_
            return hgot, "", "filled a hole with a new face", width
        why = why or hwhy
        return None, why or "no extension, trim or move closes it", "", gap
    # the cheapest change that keeps the part's shape first: the face continued or cut back along its own surface
    # (1, 2); only then its edge moved onto its neighbour (3)
    # 1. EXTENDED along its own surface, and cut where it meets its neighbour
    if got is None and not step:
        ext = _extended(a, e, EXTEND_PAST * gap, fit)
        if ext is None:
            why = why or "the face could not be continued on its own surface past that edge"
        else:
            nb = _normal_at(b, mid)
            same = na is not None and nb is not None and abs(float(np.dot(na, nb))) > math.cos(math.radians(SAME_SIDE_DEG))
            touch: list[int] = []
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
                    # AN EDGE STOPPING SHORT OF A SHEET DRAWN IN SEVERAL PATCHES (a wall's foot short of a panel):
                    # the continued face is cut where it meets that sheet - every face it reaches that is not its own
                    # neighbour - and the faces it then ends on are scored along its new edge (SURF-2's imprint)
                    touch = _cutters(here, k, ext)
                    if len(touch) > 1:
                        pieces = _split(ext, ms._compound([here.faces[j] for j in touch]))
                        new = _keep_side(pieces, a, e, here.tol) if len(pieces) > 1 else None
                if new is None:
                    why = why or "the continued face does not reach its neighbour"
            if new is not None:
                got, w = _try(here, k, new, a, band, nbr, max(2.0 * here.tol, 0.25 * gap), length_before,
                              touch=touch)
                why = w or why
                how = "extended along its own surface" + ("" if same else " and trimmed where it meets its neighbour")
                if got is not None:
                    changed = abs(ms._area(new) - ms._area(a)) / max(ms._length(e), 1e-300)
                    # an extension covers the gap and no more: a strip much wider than the gap lies over the neighbour
                    # (11 Nov: a trimmed B-spline face 1 mm short was continued 2.2 mm along a slanted cut)
                    if changed > EXTEND_PAST * gap + 2.0 * here.tol:
                        got, why = None, "continuing the face would cover more than the gap"
                    elif not less_open(got):
                        got, why = None, why or "the change would not leave less open"
    # 1b. CONTINUED BY A TANGENT STRIP where the face's own surface does not run on (or its continuation does not
    # reach): an edge stopping short of a face at an angle - a wall's foot short of a sheet - only
    if got is None and not step and na is not None and nb0 is not None and \
            abs(float(np.dot(na, nb0))) <= math.cos(math.radians(SAME_SIDE_DEG)):
        sgot, swhy, width = _with_strip(here, p, e, k, nbr, gap, band, look)
        if sgot is not None and not less_open(sgot):
            sgot, swhy = None, "the change would not leave less open"
        if sgot is not None:
            return sgot, "", "extended by a strip tangent to it and trimmed where it meets its neighbour", width
        why = why or swhy
    if got is not None:
        return got, "", how, changed
    # 2. TRIMMED back to the neighbour's edge (an overlap: the face runs over its neighbour)
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
                    if got is not None and not less_open(got):
                        got, why = None, why or "the change would not leave less open"
    if got is not None:
        return got, "", how, changed
    # 3. MOVED: the two faces sewn alone at the gap (a step, a corner off its neighbour's corner), each edge moved by
    # at most the gap
    if got is None and not in_plane:
        rows = [r for r in sc._couples(sm.Sewn([a, b], ["a", "b"], [[0, 1]], here.tol, here.diag), 1.5 * gap)[1]
                if not r["bad"]]
        if rows:
            local = [dict(r, on=(k, nbr)) for r in rows]
            got, _rec = sc._one_at_a_time(here, 1.5 * gap, local, {"places": [], "refused": ""}, length_before)
            how = "moved onto its neighbour"
            changed = gap
            if got is not None:
                for j in (k, nbr):
                    w = _check(got.faces[j], here.faces[j], band,
                               [f for i, f in enumerate(got.faces) if i not in (k, nbr)], here.tol)
                    if w:
                        got, why = None, why or w
                        break
            else:
                why = why or "the two faces would not sew at the gap"
    if got is not None and not less_open(got):
        got, why = None, why or "the change would not leave less open"
    if got is None:
        return None, why or "no extension, trim or move closes it", "", gap
    return got, "", how, changed


def reach_of(diag_mm: float, *, limit_mm: float | None = None, element_mm: float | None = None,
             element_from: str = "asked") -> tuple[float, str]:
    """SURF-12 / SURF-13: how wide a gap may be closed, in mm, and where that came from: the notes' own ("close gaps
    up to 4 mm": "notes"), else the element size (the user's, "asked", or the run's own, "run") - a gap narrower
    than one element is one an analyst closes - but never past REACH_PART of the part ("part"; `diag_mm` its
    diagonal): a gap wider than that is no place where the surfaces fail to meet but a drawn opening (SURF-8)."""
    cap = REACH_PART * float(diag_mm)
    if limit_mm:
        return float(limit_mm), "notes"
    if element_mm and float(element_mm) < cap:
        return float(element_mm), element_from
    return cap, "part"


def run_element_mm(sheets: list[dict], wall_mm: float | None) -> float | None:
    """The element size the shell run chooses for a surface part with no size asked: engines/shell_plan's base size of
    its largest sheet as a panel (SHMESH-1, SHMESH-15, SHMESH-2, at the user's wall or SURF-5's sizing wall), mm - a
    patch that is a sheet of its own only because a gap cuts it off does not set the part's size. None when there
    are no sheets."""
    from meshpipeline.engines import shell_plan as sp
    from meshpipeline.engines.surface_run import sizing_wall

    sized = [s for s in sheets if float(s.get("size_mm") or 0.0) > 0]
    if not sized:
        return None
    big = max(sized, key=lambda s: (float(s.get("area_mm2") or 0.0), float(s["size_mm"])))
    size = float(big["size_mm"])
    wall = float(wall_mm) if wall_mm else sizing_wall(size)
    return float(sp.base_size(wall=wall, size=size, width=size)[0])


def mend(sewn, *, mm_per_unit: float, limit_mm: float | None = None,
         element_mm: float | None = None, element_from: str = "asked") -> tuple[Any, dict]:
    """SURF-12: the open places of a surface part's sewn sheets closed one at a time, nearest gap first: (the Sewn,
    the record in mm). `limit_mm`: the reach the user's notes give; else `element_mm` (the user's element size,
    `element_from` "asked", or the run's own, "run"), never past REACH_PART of the part (reach_of)."""
    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad import surface_model as sm

    mm = float(mm_per_unit)
    reach_mm, reach_from = reach_of(float(sewn.diag) * mm, limit_mm=limit_mm, element_mm=element_mm,
                                    element_from=element_from)
    reach = reach_mm / mm
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
        row: dict[str, Any] = {"at_mm": [_r(c * mm) for c in p["at_mm"]], "gap_mm": _r(gap * mm)}
        # each side of the gap in turn: the face of the longest edge first, then its neighbour (a fillet's gap to the
        # main panel may be closed by changing either)
        sides: dict[int, tuple] = {}
        for m in sorted(p["_members"], key=lambda m: (-ms._length(m[0]), m[1])):
            sides.setdefault(m[1], m)
        got, how, changed, steps = None, "", gap, []
        said: dict[str, Any] = {}
        for e, k, _g, nbr in list(sides.values())[:2]:
            # the reach is the gap's own limit, whichever face changes: a gap under one element is closed (the face's
            # own width no longer holds it back - 12 Nov: 5 of a stamped training part's 8 places, beside 4 mm fillet
            # strips, were left for being wider than half a strip)
            allowed = reach
            row["faces"] = [here.names[k], here.names[nbr]]
            if gap > allowed:
                steps.append(f"{here.names[k]}: the gap is wider than the {allowed * mm:.2g} mm that may be closed")
                continue
            got, why, how, changed = _close_one(here, p, e, k, nbr, gap, allowed, look)
            if got is not None:
                break
            steps.append(f"{here.names[k]}: {why}")
        if got is None and sides and gap <= reach:
            # neither face's own change closes it: the last ways (a sliver filled, a corner's both faces continued),
            # tried once for the place; their reason, when they were tried, is the one told
            e, k, _g, nbr = next(iter(sides.values()))
            got, why, how, changed = _close_one(here, p, e, k, nbr, gap, reach, look, last=True, said=said)
            if got is None and why:
                steps.insert(0, f"{here.names[k]}: {why}")
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
        if said.get("round"):
            row["round"] = list(said["round"])
        done.append(row)
    record = {"reach_mm": _r(reach * mm), "reach_from": reach_from,
              "closed": int(sum(int(r["places_closed"]) for r in done)),
              "moved": sum(1 for r in done if r["how"].startswith("moved")),
              "extended": sum(1 for r in done if r["how"].startswith("extended")),
              "trimmed": sum(1 for r in done if r["how"].startswith("trimmed")),
              "filled": sum(1 for r in done if r["how"].startswith("filled")),
              "widest_mm": _r(max([float(r["moved_mm"]) for r in done] + [0.0])),
              "changes": done[:40], "left": len(left), "left_places": left[:40]}
    return here, record


__all__ = ["EXTEND_PAST", "REACH_PART", "mend", "reach_of", "run_element_mm"]
