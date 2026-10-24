# Responsibility: Build the small structural parts the FEA measurement tests read, each of KNOWN
# geometry (wall thickness, hole and fillet radii, notch and groove radii, inside-corner angles), as
# OpenCASCADE shapes, STEP files or STL triangles under the test's own tmp_path.
# Boundaries: generated per test, never committed. Millimetres throughout.
from __future__ import annotations

from pathlib import Path


def _box(x0, y0, z0, dx, dy, dz):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    return BRepPrimAPI_MakeBox(gp_Pnt(x0, y0, z0), dx, dy, dz).Shape()


def _cyl(x, y, z, r, h, axis=(0, 0, 1)):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, y, z), gp_Dir(*axis)), r, h).Shape()


def _unify(shape):
    from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain

    u = ShapeUpgrade_UnifySameDomain(shape, True, True, True)
    u.Build()
    return u.Shape()


def _fuse(a, b):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse

    return _unify(BRepAlgoAPI_Fuse(a, b).Shape())


def _cut(a, b):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut

    return _unify(BRepAlgoAPI_Cut(a, b).Shape())


def _edges_where(shape, test):
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_EDGE, m)
    out = []
    for i in range(1, m.Extent() + 1):
        e = TopoDS.Edge_s(m.FindKey(i))
        box = Bnd_Box()
        BRepBndLib.AddOptimal_s(e, box, False, False)
        if test(box.Get()):
            out.append(e)
    return out


def _fillet(shape, edges, r):
    from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet

    mk = BRepFilletAPI_MakeFillet(shape)
    for e in edges:
        mk.Add(r, e)
    return mk.Shape()


# ------------------------------------------------------------------- the FEA audit's parts ----
# The three parts of the FEA audit (design section 1, ~/fea_audit/make_cad.py), drawn the same way.

#: L bracket: base 80 x 40 x 8, upright 8 x 40 x 60, inside fillet 6, two 8.5 mm bolt holes
AUDIT_BRACKET = {"thickness": 8.0, "fillet_radius": 6.0, "hole_radius": 4.25, "holes": 2}


def audit_l_bracket():
    shape = _fuse(_box(0, 0, 0, 80, 40, 8), _box(0, 0, 0, 8, 40, 60))
    inner = _edges_where(shape, lambda b: abs(b[0] - 8) < 1e-3 and abs(b[3] - 8) < 1e-3
                         and abs(b[2] - 8) < 1e-3 and abs(b[5] - 8) < 1e-3)
    assert len(inner) == 1, inner
    shape = _fillet(shape, inner, 6.0)
    shape = _cut(shape, _cyl(35, 20, -1, 4.25, 10))
    return _cut(shape, _cyl(62, 20, -1, 4.25, 10))


#: plate with a hole: 200 x 100 x 5, a 20 mm hole in the middle (d/W = 0.2)
AUDIT_PLATE = {"thickness": 5.0, "hole_radius": 10.0}


def audit_plate_with_hole():
    return _cut(_box(0, 0, 0, 200, 100, 5), _cyl(100, 50, -1, 10, 7))


#: cantilever: a 300 x 20 x 20 bar
AUDIT_CANTILEVER = {"thickness": 20.0}


def audit_cantilever():
    return _box(0, 0, 0, 300, 20, 20)


# ------------------------------------------------------------------------------- the parts ----
#: plate with a hole: 120 x 60 x 4 mm, a 7.5 mm radius hole through the middle
PLATE = {"thickness": 4.0, "hole_radius": 7.5}


def plate_with_hole():
    return _cut(_box(0, 0, 0, 120, 60, 4), _cyl(60, 30, -1, 7.5, 6))


#: an L bracket: base 60 x 30 x 6, upright 6 x 30 x 40, its inside corner filleted at 4 mm (or left
#: sharp: a 90 degree inside corner, 30 mm long)
BRACKET = {"thickness": 6.0, "fillet_radius": 4.0, "corner_angle": 90.0, "corner_length": 30.0}


def bracket(fillet: float | None = 4.0):
    shape = _fuse(_box(0, 0, 0, 60, 30, 6), _box(0, 0, 0, 6, 30, 40))
    if fillet:
        inner = _edges_where(shape, lambda b: abs(b[0] - 6) < 1e-3 and abs(b[3] - 6) < 1e-3
                             and abs(b[2] - 6) < 1e-3 and abs(b[5] - 6) < 1e-3)
        assert len(inner) == 1, inner
        shape = _fillet(shape, inner, fillet)
    return shape


#: a stepped shaft along z: 40 mm diameter for 60 mm, then 24 mm diameter for 40 mm, the shoulder's
#: inside corner filleted at 2 mm, and a round-bottomed groove of 3 mm radius round the big part
SHAFT = {"thickness": 24.0, "shoulder_fillet": 2.0, "groove_radius": 3.0}


def stepped_shaft():
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeTorus
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    shape = _fuse(_cyl(0, 0, 0, 20, 60), _cyl(0, 0, 60, 12, 40))
    shoulder = _edges_where(shape, lambda b: abs(b[2] - 60) < 1e-3 and abs(b[5] - 60) < 1e-3
                            and abs(b[3] - 12) < 1e-2)
    assert len(shoulder) == 1, shoulder
    shape = _fillet(shape, shoulder, 2.0)
    groove = BRepPrimAPI_MakeTorus(gp_Ax2(gp_Pnt(0, 0, 30), gp_Dir(0, 0, 1)), 20, 3).Shape()
    return _cut(shape, groove)


#: a bar with a V notch: 100 x 20 x 30 mm, a 60 degree V cut 10 mm deep across its top
VNOTCH = {"angle": 60.0, "length": 20.0}


def v_notched_bar():
    import math

    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.gp import gp_Pnt, gp_Vec

    # the V's tip 10 mm below the top face, its sides 30 degrees either side of the vertical,
    # drawn 5 mm past the top so the cut is clean
    half = 15.0 * math.tan(math.radians(30.0))
    poly = BRepBuilderAPI_MakePolygon(gp_Pnt(50 - half, -1, 35), gp_Pnt(50, -1, 20),
                                      gp_Pnt(50 + half, -1, 35), True)
    prism = BRepPrimAPI_MakePrism(BRepBuilderAPI_MakeFace(poly.Wire()).Face(), gp_Vec(0, 22, 0)).Shape()
    return _cut(_box(0, 0, 0, 100, 20, 30), prism)


#: a 40 mm cube with a sealed 30 mm cavity in the middle: 5 mm walls, one body
HOLLOW = {"thickness": 5.0}


def hollow_box():
    return _cut(_box(0, 0, 0, 40, 40, 40), _box(5, 5, 5, 30, 30, 30))


def split_fillet_bracket():
    """The filleted bracket drawn as two 15 mm slices fused without merging their faces: the
    fillet arrives as two faces of the same 90 degree arc, one behind the other."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse

    def half(y0):
        shape = _fuse(_box(0, y0, 0, 60, 15, 6), _box(0, y0, 0, 6, 15, 40))
        inner = _edges_where(shape, lambda b: abs(b[0] - 6) < 1e-3 and abs(b[3] - 6) < 1e-3
                             and abs(b[2] - 6) < 1e-3 and abs(b[5] - 6) < 1e-3)
        return _fillet(shape, inner, 4.0)

    return BRepAlgoAPI_Fuse(half(0.0), half(15.0)).Shape()


def part_filleted_bracket():
    """The bracket with its inside corner filleted (4 mm) for y 15..30 only and left sharp for
    y 0..15: one smooth stretch of a triangle file runs from the base round the fillet to the
    upright, past the sharp 15 mm stretch."""
    def half(y0, fillet):
        shape = _fuse(_box(0, y0, 0, 60, 15, 6), _box(0, y0, 0, 6, 15, 40))
        if not fillet:
            return shape
        inner = _edges_where(shape, lambda b: abs(b[0] - 6) < 1e-3 and abs(b[3] - 6) < 1e-3
                             and abs(b[2] - 6) < 1e-3 and abs(b[5] - 6) < 1e-3)
        return _fillet(shape, inner, 4.0)

    return _fuse(half(0.0, False), half(15.0, True))


def two_blocks():
    """Two separate 10 mm cubes in one file."""
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound

    c = TopoDS_Compound()
    b = BRep_Builder()
    b.MakeCompound(c)
    b.Add(c, _box(0, 0, 0, 10, 10, 10))
    b.Add(c, _box(30, 0, 0, 10, 10, 10))
    return c


def _compound(*shapes):
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound

    c = TopoDS_Compound()
    b = BRep_Builder()
    b.MakeCompound(c)
    for sh in shapes:
        b.Add(c, sh)
    return c


def touching_blocks(size: float = 10.0):
    """Two cubes of `size` mm side by side along x, sharing the face x = size: an assembly's two
    parts that TOUCH over one face (size x size mm)."""
    return _compound(_box(0, 0, 0, size, size, size), _box(size, 0, 0, size, size, size))


def doubled_block():
    """The same 10 mm cube twice in one file (a body drawn twice): the second wholly inside the first."""
    return _compound(_box(0, 0, 0, 10, 10, 10), _box(0, 0, 0, 10, 10, 10))


def overlapping_blocks():
    """Two 10 mm cubes, the second shifted 8 mm along x: they PASS INTO each other over a 2 x 10 x 10
    mm slab (interference: 20% of either part)."""
    return _compound(_box(0, 0, 0, 10, 10, 10), _box(8, 0, 0, 10, 10, 10))


def pinned_plate():
    """A 40 x 40 x 6 mm plate with an 8 mm hole through its middle, and a pin filling the hole
    exactly: two touching bodies in one file (an assembly's bolt in its hole). The pin's seam is
    turned 7 degrees from the hole's, so their facets cross a hair apart, as two parts' do in a
    real assembly. The plate's wall is 6 mm; the pin's is 6 mm along its axis and 8 across."""
    import math

    from OCP.BRep import BRep_Builder
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.TopoDS import TopoDS_Compound

    turn = math.radians(7.0)
    pin = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(20, 20, 0), gp_Dir(0, 0, 1), gp_Dir(math.cos(turn), math.sin(turn), 0)),
                                   4, 6).Shape()

    c = TopoDS_Compound()
    b = BRep_Builder()
    b.MakeCompound(c)
    b.Add(c, _cut(_box(0, 0, 0, 40, 40, 6), _cyl(20, 20, -1, 4, 8)))
    b.Add(c, pin)
    return c


#: the beveled plate's acute edge (degrees) and thickness (mm)
BEVEL = {"angle": 75.0, "thickness": 5.0}


def beveled_plate(angle: float | None = None):
    """A 50 x 50 x 5 mm plate whose x = 0 side is beveled: it meets the bottom at `angle` degrees (75
    by default; a TAPER, the material running to nothing at that edge) and the top at 180 - angle.
    Its wall is 5 mm."""
    import math

    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.gp import gp_Pnt, gp_Vec

    t = BEVEL["thickness"]
    lean = t / math.tan(math.radians(angle if angle is not None else BEVEL["angle"]))
    poly = BRepBuilderAPI_MakePolygon()
    for x, z in ((0.0, 0.0), (50.0, 0.0), (50.0, t), (lean, t)):
        poly.Add(gp_Pnt(x, 0.0, z))
    poly.Close()
    face = BRepBuilderAPI_MakeFace(poly.Wire()).Face()
    return BRepPrimAPI_MakePrism(face, gp_Vec(0.0, 50.0, 0.0)).Shape()


def tangent_boss():
    """A 40 x 20 x 10 mm plate with a round boss (radius 5, 5 high) on top whose foot is tangent to the
    plate's y = 0 edge: on the top face the boss's circle meets that straight edge at no angle - a
    CUSP at (10, 0, 10). A sound solid: the boss and the plate's side face share only that point."""
    return _fuse(_box(0, 0, 0, 40, 20, 10), _cyl(10, 5, 10, 5, 5))


def micro_step():
    """A 20 x 10 x 10 mm block whose lower half sticks out 0.003 mm on its y = 10 side: a sliver face
    0.003 mm wide along z = 5 and two 0.003 mm edges at its ends - MICRO DETAILS, far below any element
    a part this size is meshed with."""
    return _fuse(_box(0, 0, 0, 20, 10, 10), _box(0, 0, 0, 20, 10.003, 5))


#: where the tangent boss's cusp is (mm)
TANGENT_BOSS_CUSPS = [(10.0, 0.0, 10.0)]


# ------------------------------------------------------------------------------- the files ----
def write_step(shape, path: Path) -> Path:
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    w = STEPControl_Writer()
    w.Transfer(shape, STEPControl_AsIs)
    path.parent.mkdir(parents=True, exist_ok=True)
    w.Write(str(path))
    return path


def stl_triangles(shape, chord: float = 0.02, angle: float = 0.08):
    """The shape as a fine triangle soup ((m, 3, 3) mm), the way an STL export carries it."""
    import numpy as np
    from OCP.BRep import BRep_Tool
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    BRepMesh_IncrementalMesh(shape, chord, False, angle, True)
    out = []
    ex = TopExp_Explorer(shape, TopAbs_FACE)
    while ex.More():
        f = TopoDS.Face_s(ex.Current())
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(f, loc)
        trsf = loc.Transformation()
        pts = [tri.Node(k).Transformed(trsf) for k in range(1, tri.NbNodes() + 1)]
        for k in range(1, tri.NbTriangles() + 1):
            g = tri.Triangle(k)
            ids = [g.Value(1), g.Value(2), g.Value(3)]
            if f.Orientation() == TopAbs_REVERSED:
                ids = [ids[0], ids[2], ids[1]]
            out.append([[pts[i - 1].X(), pts[i - 1].Y(), pts[i - 1].Z()] for i in ids])
        ex.Next()
    return np.asarray(out, dtype=float)


def sliver_block():
    """A 40 x 20 x 10 mm block with a 16 mm bore whose edge runs within 0.5 mm of one side: a wall
    3 mm or more everywhere but one 0.5 mm sliver (fea0221's counterbore near its bore, made simple)."""
    return _cut(_box(0, 0, 0, 40, 20, 10), _cyl(20, 11.5, -1, 8, 12))


#: a coil spring: mean coil diameter, wire diameter, pitch, active turns (millimetres)
SPRING = {"D": 30.0, "d": 4.5, "pitch": 9.0, "turns": 4.0}


def coil_spring(D: float = SPRING["D"], d: float = SPRING["d"], pitch: float = SPRING["pitch"],
                turns: float = SPRING["turns"]):
    """A helical coil spring of round wire about the z axis, its two ends cut square to the wire
    (a swept circle: no cylinder or torus face, the case PLAN-fea-7 reads from curvature)."""
    import math

    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeWire
    from OCP.BRepLib import BRepLib
    from OCP.BRepOffsetAPI import BRepOffsetAPI_MakePipeShell
    from OCP.GC import GC_MakeCircle
    from OCP.Geom import Geom_CylindricalSurface
    from OCP.Geom2d import Geom2d_Line
    from OCP.gp import gp_Ax2, gp_Ax3, gp_Dir, gp_Dir2d, gp_Pnt, gp_Pnt2d

    r = D / 2.0
    cyl = Geom_CylindricalSurface(gp_Ax3(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), r)
    # the helix on the cylinder: u (angle) and v (height) rise together, pitch per turn
    line = Geom2d_Line(gp_Pnt2d(0, 0), gp_Dir2d(2 * math.pi, pitch))
    seg = math.hypot(2 * math.pi, pitch) * turns
    edge = BRepBuilderAPI_MakeEdge(line, cyl, 0.0, seg).Edge()
    BRepLib.BuildCurves3d_s(edge)
    spine = BRepBuilderAPI_MakeWire(edge).Wire()
    # the wire's section at the helix start, square to the helix there
    t = gp_Dir(0, 2 * math.pi * r, pitch)
    circle = GC_MakeCircle(gp_Ax2(gp_Pnt(r, 0, 0), t), d / 2.0).Value()
    profile = BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(circle).Edge()).Wire()
    pipe = BRepOffsetAPI_MakePipeShell(spine)
    pipe.SetMode(gp_Dir(0, 0, 1))      # the section keeps its attitude to the coil's axis
    pipe.Add(profile)
    pipe.Build()
    pipe.MakeSolid()
    return pipe.Shape()


def wedge(angle_deg: float, length: float = 100.0, depth: float = 20.0):
    """A prism whose cross-section is a triangle with a tip of `angle_deg` at the origin: a knife edge
    of that opening, `length` long, extruded `depth` (cad/fold_back.py finds one under 1 degree)."""
    import math

    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.gp import gp_Pnt, gp_Vec

    h = length * math.tan(math.radians(angle_deg))
    poly = BRepBuilderAPI_MakePolygon(gp_Pnt(0, 0, 0), gp_Pnt(length, 0, 0), gp_Pnt(length, h, 0), True)
    face = BRepBuilderAPI_MakeFace(poly.Wire()).Face()
    return BRepPrimAPI_MakePrism(face, gp_Vec(0, 0, depth)).Shape()
