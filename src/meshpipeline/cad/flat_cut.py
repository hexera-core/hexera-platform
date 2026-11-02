# Responsibility: Tell a flat face that CUTS a passage (an opening) from a flat face of its wall,
#                 on the B-rep: how much of the face's rim runs on smoothly into a neighbouring face.
# Boundaries: pure geometry on an OCP shape already meshed (its faces carry triangulations); no
#             I/O, no policy beyond the one threshold the callers share.
# Collaborates with: cad.scout (the geometry check's openings), cad.cad_tessellate (the internal
#             staging's opening faces).
from __future__ import annotations

import math

#: A wall RUNS ON across a rim edge when the neighbouring face meets this face tangentially there
#: (their outward normals within ~20 degrees) ...
TANGENT_COS = 0.94
#: ... and keeps facing the same way over its whole extent: its normal averaged over its area has
#: at least this much along the flat's. A fillet rounding a mouth's lip is tangent at the rim too,
#: but it turns a quarter round, and its average along the end's normal is 2/pi = 0.64, so a
#: filleted mouth stays a mouth.
RUNS_ON_MEAN_COS = 0.8
#: A flat face whose rim runs on into the wall over this share of its length is a flat PIECE OF THE
#: WALL, never an opening. An opening is a cut through the fluid: the wall turns away along its
#: whole rim (s_duct_001: both ends 0%). The flat sides of a rectangular exit section blend into
#: the round-to-square transition along one edge (26% and 31% of their rims) - they were proposed
#: as two extra outlets, and staging opened the top and bottom walls as the declared ports.
RUNS_ON_SHARE = 0.15


def edge_faces(shape):
    """The edge -> faces map of a shape (what runs_on_share reads the neighbours from)."""
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    m = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, m)
    return m


def _unit(v):
    m = math.sqrt(sum(c * c for c in v))
    return tuple(c / m for c in v) if m > 0.0 else v


def _is_plane(face) -> bool:
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Plane

    try:
        return BRepAdaptor_Surface(face).GetType() == GeomAbs_Plane
    except Exception:  # noqa: BLE001 - unreadable surface: not known to be flat
        return False


def _normal_at(face, point):
    """The face's OUTWARD normal (its orientation applied) at the surface point nearest `point`."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepGProp import BRepGProp_Face
    from OCP.gp import gp_Pnt, gp_Vec
    from OCP.ShapeAnalysis import ShapeAnalysis_Surface

    uv = ShapeAnalysis_Surface(BRep_Tool.Surface_s(face)).ValueOfUV(point, 1e-7)
    p, n = gp_Pnt(), gp_Vec()
    BRepGProp_Face(face).Normal(uv.X(), uv.Y(), p, n)
    m = n.Magnitude()
    return None if m <= 0.0 else (n.X() / m, n.Y() / m, n.Z() / m)


def _mean_normal(face):
    """The face's outward normal averaged over its area, off its triangulation: NOT rescaled to
    unit length, so its length says how much of the face looks one way (1 for a flat, 2/pi along
    the axis for a fillet ring round a disc, whose sideways parts cancel). None when the face
    carries no triangulation (then the tangent test alone decides)."""
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_REVERSED
    from OCP.TopLoc import TopLoc_Location

    loc = TopLoc_Location()
    tri = BRep_Tool.Triangulation_s(face, loc)
    if tri is None or tri.NbTriangles() == 0:
        return None
    trsf = loc.Transformation()
    pts = [tri.Node(k).Transformed(trsf) for k in range(1, tri.NbNodes() + 1)]
    sx = sy = sz = area = 0.0
    for k in range(1, tri.NbTriangles() + 1):
        a, b, c = tri.Triangle(k).Get()
        pa, pb, pc = pts[a - 1], pts[b - 1], pts[c - 1]
        ux, uy, uz = pb.X() - pa.X(), pb.Y() - pa.Y(), pb.Z() - pa.Z()
        vx, vy, vz = pc.X() - pa.X(), pc.Y() - pa.Y(), pc.Z() - pa.Z()
        cx, cy, cz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        sx += cx
        sy += cy
        sz += cz
        area += math.sqrt(cx * cx + cy * cy + cz * cz)
    if area <= 0.0:
        return None
    s = (-1.0 if face.Orientation() == TopAbs_REVERSED else 1.0) / area
    return (s * sx, s * sy, s * sz)


def runs_on_share(face, faces_of_edge) -> float:
    """The share (0..1, by length) of a flat face's OUTER rim along which a neighbouring face
    carries the wall on in the same plane: tangent at the edge and facing the same way overall.
    0.0 for a cut through a passage (every neighbour turns away at the rim); a face it cannot
    read (no rim, a degenerate edge) reads 0.0, so nothing is taken away on missing evidence."""
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.BRepTools import BRepTools
    from OCP.GCPnts import GCPnts_AbscissaPoint
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    try:
        wire = BRepTools.OuterWire_s(face)
    except Exception:  # noqa: BLE001 - no outer wire: nothing to read
        return 0.0
    if wire.IsNull():
        return 0.0
    own = _mean_normal(face)
    total = smooth = 0.0
    means: list[tuple] = []                    # (neighbour face, its mean normal), read once each
    ex = TopExp_Explorer(wire, TopAbs_EDGE)
    while ex.More():
        edge = TopoDS.Edge_s(ex.Current())
        ex.Next()
        try:
            curve = BRepAdaptor_Curve(edge)
            length = float(GCPnts_AbscissaPoint.Length_s(curve))
            mid = curve.Value(0.5 * (curve.FirstParameter() + curve.LastParameter()))
        except Exception:  # noqa: BLE001 - a degenerate edge carries no rim length
            continue
        if not (length > 0.0 and math.isfinite(length)):
            continue
        total += length
        if not faces_of_edge.Contains(edge):
            continue
        n_here = _normal_at(face, mid)
        if n_here is None:
            continue
        for other_shape in faces_of_edge.FindFromKey(edge):
            other = TopoDS.Face_s(other_shape)
            if other.IsSame(face) or _is_plane(other):
                # a flat neighbour in the same plane is the SAME flat drawn in two pieces (a mouth
                # split at a symmetry plane), not the wall running on: only a curved face carries
                # the passage's wall on past a flat
                continue
            n_other = _normal_at(other, mid)
            if n_other is None or sum(a * b for a, b in zip(n_here, n_other)) < TANGENT_COS:
                continue
            hit = next((m for f, m in means if f.IsSame(other)), False)
            mean = _mean_normal(other) if hit is False else hit
            if hit is False:
                means.append((other, mean))
            ref = _unit(own) if own is not None else n_here
            if mean is not None and sum(a * b for a, b in zip(ref, mean)) < RUNS_ON_MEAN_COS:
                continue                       # tangent at the rim but turning away: a fillet
            smooth += length
            break
    return smooth / total if total > 0.0 else 0.0


def is_wall_flat(face, faces_of_edge) -> bool:
    """A flat face that is a piece of the wall, not an opening (RUNS_ON_SHARE)."""
    return runs_on_share(face, faces_of_edge) >= RUNS_ON_SHARE
