# Responsibility: Read an IGES file the one way the product reads it: an OpenCascade IGES reader with the
# import settings that keep a spline face's boundary in one piece, so every later step works on the
# file's own edges instead of ten times as many.
# Boundaries: builds and configures the reader, and reads a file to one shape - with each boundary curve that
# turns a corner inside itself cut at that corner (MESH-26); it names no unit and judges nothing.
# cad/repair/* keeps its own reader (another owner's).
from __future__ import annotations

import contextlib
import functools
import logging
import math
import tempfile
from collections.abc import Iterator
from pathlib import Path

logger = logging.getLogger(__name__)

#: OpenCascade's IGES import splits every B-spline curve and surface at each place it is less
#: smooth than C1 (`read.iges.bspline.continuity` 1, its default). A pre-processor's export of
#: 231 trimmed spline faces came in with 8,415 edges that way, and stitching them took 190 s;
#: read with 0 (keep each B-spline whole) it had about 800 edges and stitched in under 2 s -
#: the same 231 faces and the same area. 0 keeps the file's own curves exactly as written.
BSPLINE_CONTINUITY = 0
_CONTINUITY_KEY = "read.iges.bspline.continuity"

#: MESH-26 (rulebook 81, cut only at corners since 89): A BOUNDARY CURVE THAT TURNS BY MORE THAN THIS MANY DEGREES
#: AT ONE OF ITS C0 BREAKS TURNS A CORNER there, and after the read its edge is cut at that break - and only there.
#: Kept whole, such a curve is ONE edge round a corner: a face bounded by one closed curve through four right
#: angles cannot be sewn to its four neighbours (a public pipe: open, was closed), and a polyline's face gets a
#: re-approximated boundary (a public ribbon: its 6.7 mm wall no longer found). On the 123-file public IGES corpus
#: every break that turns at all turns by 0.19 degrees or more (sampled polylines, a closed rectangle's corners, a
#: hull's 28-108 degree knuckles: 8 files), and every other break - the thousands on the spline exports the whole
#: reading was made for - by under 0.031 degrees (tangent: only the parameter speed jumps). 0.1 lies in that gap.
#: Rulebook 81 split every break of the cornered faces and of every face sharing any broken edge with them: a
#: 231-face export went from 1,340 edges to 8,262 and its shell mesh no longer finished.
CORNER_TURN_DEG = 0.1
#: the rule behind the split (docs/RULES.md)
CORNER_RULE = "MESH-26"


def is_iges(path) -> bool:
    return Path(path).suffix.lower() in (".iges", ".igs")


@functools.cache
def _reader_class():
    from OCP.IGESControl import IGESControl_Reader

    class _Reader(IGESControl_Reader):
        """OpenCascade's IGES reader whose shapes come with MESH-26's corner faces split: every product read of
        an IGES (OneShape, or Shape) gets the same shape."""

        def OneShape(self):
            return split_corner_faces(super().OneShape())

        def Shape(self, num: int = 1):
            return split_corner_faces(super().Shape(num))

    return _Reader


def iges_reader():
    """An IGESControl_Reader set to keep B-splines whole, whose shapes have MESH-26's corner faces split. The
    setting is made AFTER the reader is built: building the first one initialises OpenCascade's IGES parameters,
    and a value set before that is silently overwritten. Raises RuntimeError when OpenCascade does not take it."""
    from OCP.Interface import Interface_Static

    reader = _reader_class()()
    if not Interface_Static.SetIVal_s(_CONTINUITY_KEY, BSPLINE_CONTINUITY):
        raise RuntimeError(f"OpenCascade refused {_CONTINUITY_KEY}={BSPLINE_CONTINUITY}")
    return reader


def continuity_in_force() -> int:
    """The B-spline continuity OpenCascade's IGES import uses now (for tests and logs)."""
    from OCP.Interface import Interface_Static

    return int(Interface_Static.IVal_s(_CONTINUITY_KEY))


def _corner_breaks(curve, first: float, last: float) -> list[tuple[float, float]]:
    """(parameter, turn in degrees) at each C0 break (an inner knot of full multiplicity) of a B-spline curve
    inside [first, last]: the turn is the angle between its tangents either side. [] for any other curve."""
    from OCP.Geom import Geom_BSplineCurve, Geom_TrimmedCurve
    from OCP.gp import gp_Pnt, gp_Vec

    while isinstance(curve, Geom_TrimmedCurve):
        curve = curve.BasisCurve()
    if not isinstance(curve, Geom_BSplineCurve):
        return []
    lo, hi, degree, breaks = min(first, last), max(first, last), curve.Degree(), []
    for i in range(2, curve.NbKnots()):
        u = curve.Knot(i)
        if curve.Multiplicity(i) < degree or not lo < u < hi:
            continue
        p, before, after = gp_Pnt(), gp_Vec(), gp_Vec()
        curve.LocalD1(u, i - 1, i, p, before)
        curve.LocalD1(u, i, i + 1, p, after)
        stopped = min(before.Magnitude(), after.Magnitude()) <= 0.0
        breaks.append((u, 180.0 if stopped else math.degrees(before.Angle(after))))
    return breaks


def _corner_params(edge) -> list[float]:
    """The parameters, in order, at which `edge`'s 3D curve turns a corner: its C0 breaks that turn by more than
    CORNER_TURN_DEG. [] for an edge with none (or no 3D curve)."""
    from OCP.BRep import BRep_Tool

    if BRep_Tool.Degenerated_s(edge):
        return []
    first, last = BRep_Tool.Range_s(edge)
    curve = BRep_Tool.Curve_s(edge, 0.0, 1.0)
    if curve is None:
        return []
    return sorted(u for u, turn in _corner_breaks(curve, first, last) if turn > CORNER_TURN_DEG)


def split_corner_faces(shape):
    """MESH-26: `shape` with every boundary edge whose curve turns a corner (a break turning by more than
    CORNER_TURN_DEG) cut at those corners and nowhere else. Each piece keeps the edge's own 3D curve and its own
    curves on its faces, trimmed to the piece, so every other break - one where the curve runs on tangent - stays
    inside one edge, as read. A shared edge is cut once, for all its faces; the faces' surfaces are not touched,
    and every other edge and face is left exactly as read. The shape as it is when no curve turns a corner, or
    the cut fails or leaves a face invalid that was valid (logged)."""
    if shape is None or shape.IsNull():
        return shape
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    owners = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, owners)
    cuts = []
    for i in range(1, owners.Extent() + 1):
        edge = TopoDS.Edge_s(owners.FindKey(i))
        at = _corner_params(edge)
        if at:
            faces: list = []
            for face in owners.FindFromIndex(i):
                if not any(face.IsSame(f) for f in faces):
                    faces.append(TopoDS.Face_s(face))
            cuts.append((edge, at, faces))
    if not cuts:
        return shape
    try:
        out = _cut_edges(shape, cuts)
    except Exception as exc:  # noqa: BLE001 - the reading as it is stands, never a new failure
        logger.warning("iges read: corner edges left whole (%s: %s)", type(exc).__name__, str(exc)[:200])
        return shape
    logger.info("iges read: %d boundary edge(s) cut at the %d corner(s) their curves turn (MESH-26)",
                len(cuts), sum(len(at) for _, at, _ in cuts))
    return out


def _cut_edges(shape, cuts):
    """`shape` with each (edge, corner parameters, its faces) of `cuts` replaced by its pieces (one replacement
    per edge, so an edge two faces share is cut once for both). Raises IgesReadError when a face the cut changes
    was valid as read and is not after."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepTools import BRepTools_ReShape
    from OCP.TopAbs import TopAbs_FORWARD
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    builder, reshape = BRep_Builder(), BRepTools_ReShape()
    reshape.ModeConsiderLocation = True          # an edge placed twice is cut once, each placement kept
    changed: list = []
    for edge, at, faces in cuts:
        bare = TopoDS.Edge_s(edge.Oriented(TopAbs_FORWARD).Located(TopLoc_Location()))
        reshape.Replace(bare, _pieces(builder, edge, at, faces))     # the pieces run the way the curve runs
        changed.extend(f for f in faces if not any(f.IsSame(g) for g in changed))
    out = reshape.Apply(shape)
    if out is None or out.IsNull():
        raise IgesReadError("the cut left no shape")
    for face in changed:
        if BRepCheck_Analyzer(face).IsValid() and not BRepCheck_Analyzer(reshape.Value(face)).IsValid():
            raise IgesReadError("a face the cut changed is no longer valid")
    return out


def _pieces(builder, edge, at, faces):
    """A compound of `edge` cut at the 3D-curve parameters `at`, in the edge's own frame and orientation: each
    piece is the edge's 3D curve over its span, with each face's curve(s) on it over the same span (a seam keeps
    both), the edge's tolerance and flags, and the edge's own end vertices at its ends."""
    from OCP.BRep import BRep_Tool
    from OCP.gp import gp_Pnt2d
    from OCP.TopAbs import TopAbs_FORWARD, TopAbs_REVERSED
    from OCP.TopExp import TopExp
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS, TopoDS_Compound, TopoDS_Edge, TopoDS_Vertex

    fwd = TopoDS.Edge_s(edge.Oriented(TopAbs_FORWARD))
    placed_at = fwd.Location()
    bare = TopoDS.Edge_s(fwd.Located(TopLoc_Location()))
    curve_loc = TopLoc_Location()
    curve = BRep_Tool.Curve_s(bare, curve_loc, 0.0, 1.0)
    first, last = BRep_Tool.Range_s(bare)
    tol = BRep_Tool.Tolerance_s(bare)
    same_parameter, same_range = BRep_Tool.SameParameter_s(bare), BRep_Tool.SameRange_s(bare)
    on = []                                       # (face, curve on it, the seam's second curve or None, its range)
    for face in faces:
        c1 = BRep_Tool.CurveOnSurface_s(fwd, face, 0.0, 1.0)
        if c1 is None:
            continue
        c2 = (BRep_Tool.CurveOnSurface_s(TopoDS.Edge_s(fwd.Oriented(TopAbs_REVERSED)), face, 0.0, 1.0)
              if BRep_Tool.IsClosed_s(fwd, face) else None)
        on.append((face, c1, c2, BRep_Tool.Range_s(fwd, face), BRep_Tool.Surface_s(face)))

    def on_face(u, rng):                          # the face curve's parameter at 3D parameter u
        return rng[0] + (u - first) * (rng[1] - rng[0]) / (last - first)

    ts = [first, *at, last]
    verts = [TopExp.FirstVertex_s(bare)]
    for u in at:
        p = curve.Value(u).Transformed(curve_loc.Transformation())
        where = p.Transformed(placed_at.Transformation())
        gap = tol
        for _face, c1, _c2, rng, surface in on:
            uv = gp_Pnt2d()
            c1.D0(on_face(u, rng), uv)
            gap = max(gap, surface.Value(uv.X(), uv.Y()).Distance(where))
        v = TopoDS_Vertex()
        builder.MakeVertex(v, p, gap)
        verts.append(v)
    verts.append(TopExp.LastVertex_s(bare))
    comp = TopoDS_Compound()
    builder.MakeCompound(comp)
    for k in range(len(ts) - 1):
        piece = TopoDS_Edge()
        builder.MakeEdge(piece, curve, curve_loc, tol)
        builder.Add(piece, verts[k].Oriented(TopAbs_FORWARD))
        builder.Add(piece, verts[k + 1].Oriented(TopAbs_REVERSED))
        builder.Range(piece, ts[k], ts[k + 1], True)
        placed = TopoDS.Edge_s(piece.Located(placed_at))
        for face, c1, c2, rng, _surface in on:
            if c2 is None:
                builder.UpdateEdge(placed, c1, face, tol)
            else:
                builder.UpdateEdge(placed, c1, c2, face, tol)
            builder.Range(placed, face, on_face(ts[k], rng), on_face(ts[k + 1], rng))
        builder.SameRange(piece, same_range)
        builder.SameParameter(piece, same_parameter)
        gap = max((_gap(curve, curve_loc, placed_at, c1, rng, surface, on_face, ts[k], ts[k + 1])
                   for _face, c1, _c2, rng, surface in on), default=0.0)
        if gap > tol:                             # a piece is checked closer than the whole edge was (BRepCheck's
            builder.UpdateEdge(piece, gap * 1.01)  # 23 points over a shorter span): its tolerance covers its gap
        builder.Add(comp, piece)
    return comp


def _gap(curve, curve_loc, placed_at, c2d, rng, surface, on_face, t0: float, t1: float, n: int = 44) -> float:
    """The largest distance between a piece's 3D curve and its curve on a face over [t0, t1], at n + 1 even steps
    (44 steps hold the 23 points BRepCheck samples)."""
    from OCP.gp import gp_Pnt2d

    trsf = placed_at.Multiplied(curve_loc).Transformation()
    worst, uv = 0.0, gp_Pnt2d()
    for i in range(n + 1):
        t = t0 + (t1 - t0) * i / n
        c2d.D0(on_face(t, rng), uv)
        worst = max(worst, curve.Value(t).Transformed(trsf).Distance(surface.Value(uv.X(), uv.Y())))
    return worst


class IgesReadError(RuntimeError):
    """The IGES file could not be read to a shape."""


def read_iges(path):
    """The shape in an IGES file, as read (unscaled, unmeshed). Raises IgesReadError when the file
    cannot be read; the shape may still be empty when the file holds nothing transferable."""
    from OCP.IFSelect import IFSelect_RetDone

    reader = iges_reader()
    if reader.ReadFile(str(path)) != IFSelect_RetDone:
        raise IgesReadError(f"OpenCASCADE could not read the IGES file {Path(path).name}")
    reader.TransferRoots()
    return reader.OneShape()


@contextlib.contextmanager
def gmsh_import_path(path) -> Iterator[Path]:
    """The file gmsh should import for an upload. gmsh carries its own OpenCascade, whose IGES import splits every
    B-spline whatever this process sets (a 1,448-edge part through gmsh is 468 edges here), so an IGES is read
    here and handed to gmsh as a BREP of the same shape - in the same millimetres gmsh's own IGES import gives, so
    its Geometry.OCCScaling stages it at the same size. Any other file, or an IGES this cannot read or write, is
    yielded as it is (logged): staged exactly as before, never a new failure. The BREP lives only while gmsh
    imports it."""
    path = Path(path)
    if not is_iges(path):
        yield path
        return
    with tempfile.TemporaryDirectory(prefix="iges_for_gmsh_") as tmp:
        brep = Path(tmp) / (path.stem + ".brep")
        try:
            from OCP.BRepTools import BRepTools

            shape = read_iges(path)
            if shape is None or shape.IsNull():
                raise IgesReadError("the IGES file holds no shape")
            if not BRepTools.Write_s(shape, str(brep)) or not brep.exists():
                raise IgesReadError("the BREP could not be written")
        except Exception as exc:  # noqa: BLE001 - gmsh imports the IGES itself, as before
            logger.warning("iges read: %s handed to gmsh as it is (%s: %s)", path.name, type(exc).__name__,
                           str(exc)[:200])
            yield path
            return
        yield brep


__all__ = ["BSPLINE_CONTINUITY", "CORNER_TURN_DEG", "IgesReadError", "continuity_in_force", "gmsh_import_path",
           "iges_reader", "is_iges", "read_iges", "split_corner_faces"]
