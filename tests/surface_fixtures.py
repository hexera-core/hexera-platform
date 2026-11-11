# Responsibility: Small SURFACE parts drawn with OpenCASCADE for the surface-file tests (SURF-1..6): open sheets (a
# plate with a hole, a bent bracket's mid-surface, an open tube, a tank skin with a pinhole), files written unsewn, a
# closed skin, a skin with a crack - and the shape helpers they share.
# Boundaries: test data only; millimetres; nothing here is read from a file.
from __future__ import annotations

import math


def _pnt(p):
    from OCP.gp import gp_Pnt

    return gp_Pnt(float(p[0]), float(p[1]), float(p[2]))


def faces_of(shape) -> list:
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, m)
    return [TopoDS.Face_s(m.FindKey(i)) for i in range(1, m.Extent() + 1)]


def compound(shapes):
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound

    c = TopoDS_Compound()
    b = BRep_Builder()
    b.MakeCompound(c)
    for s in shapes:
        b.Add(c, s)
    return c


def sewn(shapes, tol: float = 1e-6):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing

    sw = BRepBuilderAPI_Sewing(tol)
    for s in shapes:
        sw.Add(s)
    sw.Perform()
    return sw.SewedShape()


def unsewn(shape):
    """Every face copied apart: a file written with no shared edge."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy

    return compound([BRepBuilderAPI_Copy(f).Shape() for f in faces_of(shape)])


def moved(shape, v):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Trsf, gp_Vec

    t = gp_Trsf()
    t.SetTranslation(gp_Vec(float(v[0]), float(v[1]), float(v[2])))
    return BRepBuilderAPI_Transform(shape, t, True).Shape()


def rect(x0, y0, x1, y1, z: float = 0.0):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon

    poly = BRepBuilderAPI_MakePolygon()
    for p in ((x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z)):
        poly.Add(_pnt(p))
    poly.Close()
    return BRepBuilderAPI_MakeFace(poly.Wire(), True).Face()


def _cut(shape, tool):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut

    return BRepAlgoAPI_Cut(shape, tool).Shape()


def _cyl(base, axis, r, h):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir

    return BRepPrimAPI_MakeCylinder(gp_Ax2(_pnt(base), gp_Dir(*map(float, axis))), float(r), float(h)).Shape()


def plate(length: float = 200.0, width: float = 50.0):
    """A flat sheet: one face."""
    return rect(0, 0, length, width)


def plate_with_hole(length: float = 200.0, width: float = 100.0, r: float = 10.0):
    return _cut(rect(0, 0, length, width), _cyl((length / 2, width / 2, -5), (0, 0, 1), r, 10))


def bracket(a: float = 60.0, b: float = 40.0, width: float = 50.0, r: float = 4.0):
    """A bent bracket as its mid-surface: two flat flanges and the bend between them (three faces, one sheet), a
    hole in the long flange."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeWire
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.GC import GC_MakeArcOfCircle
    from OCP.gp import gp_Vec

    mk = BRepBuilderAPI_MakeWire()
    mk.Add(BRepBuilderAPI_MakeEdge(_pnt((a, 0, 0)), _pnt((r, 0, 0))).Edge())
    arc = GC_MakeArcOfCircle(_pnt((r, 0, 0)), gp_Vec(-1, 0, 0), _pnt((0, 0, r))).Value()
    mk.Add(BRepBuilderAPI_MakeEdge(arc).Edge())
    mk.Add(BRepBuilderAPI_MakeEdge(_pnt((0, 0, r)), _pnt((0, 0, b))).Edge())
    shell = BRepPrimAPI_MakePrism(mk.Wire(), gp_Vec(0, width, 0)).Shape()
    return sewn([_cut(shell, _cyl((a * 0.6, width / 2, -5), (0, 0, 1), 5.0, 10))])


def open_tube(r: float = 10.0, length: float = 300.0):
    """A tube's wall alone, open at both ends."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder

    return max(faces_of(BRepPrimAPI_MakeCylinder(float(r), float(length)).Shape()),
               key=lambda f: _area(f))


def _area(shape) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    g = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, g)
    return abs(float(g.Mass()))


def box_solid(a: float = 60.0, b: float = 40.0, c: float = 20.0):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    return BRepPrimAPI_MakeBox(float(a), float(b), float(c)).Shape()


def closed_skin(a: float = 60.0, b: float = 40.0, c: float = 20.0):
    """A box's six faces with no solid: faces that close."""
    return compound(faces_of(box_solid(a, b, c)))


def skin_with_pinhole(a: float = 60.0, b: float = 40.0, c: float = 20.0, r: float = 1.5):
    """A box's skin with one small round hole in a face: a closed skin with a gap (0.1% of its surface)."""
    return sewn([_cut(sewn(faces_of(box_solid(a, b, c))), _cyl((a / 2, b / 2, c - 2), (0, 0, 1), r, 4))])


def skin_with_crack(gap: float, a: float = 60.0, b: float = 40.0, c: float = 20.0):
    """A box's faces with its top face lifted `gap` off the rest: a seam that does not sew."""
    faces = faces_of(box_solid(a, b, c))
    top = max(range(len(faces)), key=lambda k: _centre_z(faces[k]))
    return compound([moved(f, (0, 0, gap)) if k == top else f for k, f in enumerate(faces)])


def _centre_z(face) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    g = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, g)
    return float(g.CentreOfMass().Z())


def tray(a: float = 80.0, b: float = 50.0, c: float = 20.0):
    """A box without its lid: five faces."""
    faces = faces_of(box_solid(a, b, c))
    top = max(range(len(faces)), key=lambda k: _centre_z(faces[k]))
    return sewn([f for k, f in enumerate(faces) if k != top])


def tee(split: bool = True, length: float = 200.0, half: float = 50.0, height: float = 40.0):
    """A plate with an upright web along its middle, as surfaces. `split`: the plate is drawn as two faces that
    meet under the web (three faces at one line); else as one face, the web's foot landing inside it."""
    import math as _m

    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf

    t = gp_Trsf()
    t.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(1, 0, 0)), _m.pi / 2)
    web = BRepBuilderAPI_Transform(rect(0, 0, length, height), t, True).Shape()
    plate_faces = [rect(0, -half, length, 0), rect(0, 0, length, half)] if split else [rect(0, -half, length, half)]
    return compound([*plate_faces, web])


def write_step(shape, path) -> str:
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    w = STEPControl_Writer()
    Interface_Static.SetCVal_s("write.step.unit", "MM")
    w.Transfer(shape, STEPControl_AsIs)
    w.Write(str(path))
    return str(path)


def write_iges(shape, path) -> str:
    from OCP.IGESControl import IGESControl_Writer

    w = IGESControl_Writer("MM", 0)
    w.AddShape(shape)
    w.ComputeModel()
    w.Write(str(path))
    return str(path)


def perimeter_of_plate_with_hole(length: float = 200.0, width: float = 100.0, r: float = 10.0) -> float:
    return 2.0 * (length + width) + 2.0 * math.pi * r
