# Responsibility: Read an IGES file the one way the product reads it: an OpenCascade IGES reader with the
# import settings that keep a spline face's boundary in one piece, so every later step works on the
# file's own edges instead of ten times as many.
# Boundaries: builds and configures the reader, and reads a file to one shape - with the faces whose boundary
# turns a corner inside one curve split at that curve's breaks (MESH-26); it names no unit and judges nothing.
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

#: MESH-26 (rulebook 81): A BOUNDARY CURVE THAT TURNS BY MORE THAN THIS MANY DEGREES AT ONE OF ITS C0 BREAKS
#: TURNS A CORNER there, and every face it bounds is split at its curves' breaks after the read, as OpenCascade's
#: own import (continuity 1) splits them. Kept whole, such a curve is ONE edge round a corner: a face bounded by
#: one closed curve through four right angles cannot be sewn to its four neighbours (a public pipe: open, was
#: closed), and a polyline's face gets a re-approximated boundary (a public ribbon: its 6.7 mm wall no longer
#: found). On the 123-file public IGES corpus every break that turns at all turns by 0.19 degrees or more
#: (sampled polylines, a closed rectangle's corners, a hull's 28-108 degree knuckles: 8 files), and every other
#: break - the thousands on the spline exports the whole reading was made for - by under 0.031 degrees
#: (tangent: only the parameter speed jumps). 0.1 lies in that gap.
CORNER_TURN_DEG = 0.1


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


def _corner_turns(curve, first: float, last: float) -> list[float]:
    """The turn, in degrees, at each C0 break (an inner knot of full multiplicity) of a B-spline curve inside
    [first, last]: the angle between its tangents either side. [] for any other curve."""
    from OCP.Geom import Geom_BSplineCurve, Geom_TrimmedCurve
    from OCP.gp import gp_Pnt, gp_Vec

    while isinstance(curve, Geom_TrimmedCurve):
        curve = curve.BasisCurve()
    if not isinstance(curve, Geom_BSplineCurve):
        return []
    lo, hi, degree, turns = min(first, last), max(first, last), curve.Degree(), []
    for i in range(2, curve.NbKnots()):
        u = curve.Knot(i)
        if curve.Multiplicity(i) < degree or not lo < u < hi:
            continue
        p, before, after = gp_Pnt(), gp_Vec(), gp_Vec()
        curve.LocalD1(u, i - 1, i, p, before)
        curve.LocalD1(u, i, i + 1, p, after)
        stopped = min(before.Magnitude(), after.Magnitude()) <= 0.0
        turns.append(180.0 if stopped else math.degrees(before.Angle(after)))
    return turns


def _edge_turns(edge) -> list[float]:
    from OCP.BRep import BRep_Tool

    first, last = BRep_Tool.Range_s(edge)
    curve = BRep_Tool.Curve_s(edge, 0.0, 1.0)
    return [] if curve is None else _corner_turns(curve, first, last)


def split_corner_faces(shape):
    """MESH-26: `shape` with every face whose boundary turns a corner (a break of one of its curves turning by
    more than CORNER_TURN_DEG) split at its boundary curves' breaks - OpenCascade's own split (its 3D curves and
    curves on the face made C1, its surface left as it is: the import at continuity 1 splits only the curves).
    A face sharing a broken edge with one of those is split with it, so a shared edge stays one edge. Every other
    face is left exactly as read. The shape as it is when nothing turns a corner, or the split fails (logged)."""
    if shape is None or shape.IsNull():
        return shape
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp, TopExp_Explorer
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    owners = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, owners)
    turns = [_edge_turns(TopoDS.Edge_s(owners.FindKey(i))) for i in range(1, owners.Extent() + 1)]
    corner = [i for i, t in enumerate(turns, 1) if t and max(t) > CORNER_TURN_DEG]
    if not corner:
        return shape
    faces: list = []
    todo = [f for i in corner for f in owners.FindFromIndex(i)]
    while todo:
        face = todo.pop()
        if any(face.IsSame(f) for f in faces):
            continue
        faces.append(face)
        ex = TopExp_Explorer(face, TopAbs_EDGE)
        while ex.More():
            i = owners.FindIndex(ex.Current())
            if i and turns[i - 1]:              # a broken edge this face shares: its other faces are split too
                todo.extend(owners.FindFromIndex(i))
            ex.Next()
    try:
        split = _split_faces(shape, faces)
    except Exception as exc:  # noqa: BLE001 - the reading as it is stands, never a new failure
        logger.warning("iges read: corner faces left whole (%s: %s)", type(exc).__name__, str(exc)[:200])
        return shape
    logger.info("iges read: %d face(s) whose boundary turns a corner split at their curves' breaks (MESH-26)",
                len(faces))
    return split


def _split_faces(shape, faces):
    """`shape` with `faces` split by ShapeUpgrade_ShapeDivideContinuity: the faces are divided together (one
    context, so an edge two of them share is split once), and that context is applied to the whole shape."""
    from OCP.BRep import BRep_Builder
    from OCP.GeomAbs import GeomAbs_C0, GeomAbs_C1
    from OCP.ShapeUpgrade import ShapeUpgrade_ShapeDivideContinuity
    from OCP.TopoDS import TopoDS_Compound

    builder, comp = BRep_Builder(), TopoDS_Compound()
    builder.MakeCompound(comp)
    for face in faces:
        builder.Add(comp, face)
    divide = ShapeUpgrade_ShapeDivideContinuity(comp)
    divide.SetBoundaryCriterion(GeomAbs_C1)
    divide.SetPCurveCriterion(GeomAbs_C1)
    divide.SetSurfaceCriterion(GeomAbs_C0)
    if not divide.Perform():
        return shape
    out = divide.GetContext().Apply(shape)
    if out is None or out.IsNull():
        raise IgesReadError("the split left no shape")
    return out


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
