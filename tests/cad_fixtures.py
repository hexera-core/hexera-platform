# Responsibility: Generate the tiny CAD and surface files tests need, on demand under the test's own tmp_path.
# Boundaries: generated per test, never committed - a fixture that outlives its test is state, not input.
from __future__ import annotations

from pathlib import Path

SIDE = 10.0          # 10 coordinate units, whatever the file says those units are


def write_step(path: Path, unit: str) -> Path:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    box = BRepPrimAPI_MakeBox(SIDE, SIDE, SIDE).Shape()
    # Construct the writer BEFORE setting the unit. `write.step.unit` is registered by the STEP
    # controller's initialisation, and SetCVal_s against an unregistered static does not raise -
    # it returns False and leaves the value empty, so OCC writes its default millimetre. Setting
    # it first therefore produced a file in the WRONG unit whenever this helper was the process's
    # first STEP contact, and the right one once any earlier test had touched STEP. The unit a
    # fixture claims must never depend on test order, so the result is checked rather than
    # assumed.
    w = STEPControl_Writer()
    if not Interface_Static.SetCVal_s("write.step.unit", unit):
        raise RuntimeError(
            f"OCC refused write.step.unit={unit!r}; the STEP controller is not initialised, so "
            "this fixture would silently emit millimetres instead.")
    w.Transfer(box, STEPControl_AsIs)
    path.parent.mkdir(parents=True, exist_ok=True)
    w.Write(str(path))
    return path


def write_iges(path: Path, unit: str) -> Path:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.IGESControl import IGESControl_Writer

    box = BRepPrimAPI_MakeBox(SIDE, SIDE, SIDE).Shape()
    path.parent.mkdir(parents=True, exist_ok=True)
    w = IGESControl_Writer(unit, 0)
    w.AddShape(box)
    w.Write(str(path))
    return path


def corrupt_step_unit(src: Path, dest: Path, how: str) -> Path:
    import re
    text = src.read_text(errors="replace")
    replacement = {"malformed": "SI_UNIT(.NOPE.,.METRE.)", "missing": "SI_UNIT($,$)"}[how]
    text = re.sub(r"SI_UNIT\((\.MILLI\.|\.CENTI\.|\$),\.METRE\.\)", replacement, text, count=1)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text)
    return dest


def write_iges_with_scale(src: Path, dest: Path, scale: str = "2.0") -> Path:
    lines = src.read_text().splitlines()
    globals_ = [ln for ln in lines if len(ln) > 72 and ln[72] == "G"]
    fields = "".join(ln[:72] for ln in globals_).split(",")
    fields[12] = scale                       # parameter 13 is the model scale
    payload = ",".join(fields)
    rebuilt, i, n = [], 0, 0
    while i < len(payload):
        n += 1
        rebuilt.append(f"{payload[i:i + 72]:<72}G{n:>7}")
        i += 72
    others = [ln for ln in lines if not (len(ln) > 72 and ln[72] == "G")]
    at = next(i for i, ln in enumerate(others) if len(ln) > 72 and ln[72] == "D")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(others[:at] + rebuilt + others[at:]) + "\n")
    return dest


def write_stl(path: Path, side: float = SIDE) -> Path:
    tri = ((0.0, 0.0, 0.0), (side, 0.0, 0.0), (0.0, side, 0.0))
    lines = ["solid box", "facet normal 0 0 1", "  outer loop"]
    lines += [f"    vertex {v[0]!r} {v[1]!r} {v[2]!r}" for v in tri]
    lines += ["  endloop", "endfacet", "endsolid box"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path


def write_vtp(path: Path, side: float = SIDE) -> Path:
    import numpy as np
    import pyvista as pv

    pts = np.array([[0.0, 0.0, 0.0], [side, 0.0, 0.0], [0.0, side, 0.0]], dtype=float)
    faces = np.array([3, 0, 1, 2])
    mesh = pv.PolyData(pts, faces)
    mesh.point_data["marker"] = np.array([1.0, 2.0, 3.0])
    mesh.cell_data["region"] = np.array([7.0])
    path.parent.mkdir(parents=True, exist_ok=True)
    mesh.save(str(path))
    return path


def stl_extent(path: Path) -> float:
    from meshpipeline.cad.stl_io import read_stl_triangles
    xs = [v[0] for tri in read_stl_triangles(Path(path)) for v in tri]
    return max(xs) - min(xs)


def vtp_extent(path: Path) -> float:
    import pyvista as pv
    b = pv.read(str(path)).bounds
    return b[1] - b[0]


def write_step_of_units(path: Path, unit: str, units: float = SIDE) -> Path:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    metres_per = {"MM": 1e-3, "CM": 1e-2, "INCH": 0.0254, "M": 1.0}[unit]
    side_mm = units * metres_per * 1000.0          # OCC builds in its own millimetres
    box = BRepPrimAPI_MakeBox(side_mm, side_mm, side_mm).Shape()

    w = STEPControl_Writer()
    if not Interface_Static.SetCVal_s("write.step.unit", unit):
        raise RuntimeError(f"OCC refused write.step.unit={unit!r}")
    w.Transfer(box, STEPControl_AsIs)
    path.parent.mkdir(parents=True, exist_ok=True)
    w.Write(str(path))
    return path


def write_open_shell_step(path: Path, unit: str = "MM", side: float = SIDE) -> Path:
    """A STEP that OpenCASCADE genuinely calls INVALID: a box with one face missing.

    THE BROKEN FIXTURE THIS SUITE SAID COULD NOT BE AUTHORED. The earlier claim - that every shape
    the kernel's constructors build is sound by construction - is wrong, and this is the
    counter-example: build a box, sew five of its six faces into a shell, and declare that shell a
    solid. BRepCheck_Analyzer reports IsValid() == False, and ShapeAnalysis_FreeBounds localises
    the opening to the four edges that bounded the face that is gone.

    It is a REAL defect class, not a contrivance: a surface model exported without one patch, or a
    translation that dropped a face, arrives looking exactly like this. What it is not is a
    substitute for a licensed customer file - it has one clean defect, and real files have many
    interacting ones.
    """
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid, BRepBuilderAPI_Sewing
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    box = BRepPrimAPI_MakeBox(side, side, side).Shape()
    faces = []
    explorer = TopExp_Explorer(box, TopAbs_FACE)
    while explorer.More():
        faces.append(TopoDS.Face_s(explorer.Current()))
        explorer.Next()

    sewing = BRepBuilderAPI_Sewing(1e-6)
    for face in faces[:-1]:          # every face but one
        sewing.Add(face)
    sewing.Perform()
    maker = BRepBuilderAPI_MakeSolid()
    maker.Add(TopoDS.Shell_s(sewing.SewedShape()))
    holed = maker.Solid()

    writer = STEPControl_Writer()
    if not Interface_Static.SetCVal_s("write.step.unit", unit):
        raise RuntimeError(
            f"OCC refused write.step.unit={unit!r}; the STEP controller is not initialised, so "
            "this fixture would silently emit millimetres instead.")
    writer.Transfer(holed, STEPControl_AsIs)
    path.parent.mkdir(parents=True, exist_ok=True)
    writer.Write(str(path))
    return path


def write_missing_bore_wall_step(path: Path, unit: str = "MM", profile: str = "round") -> Path:
    """A drilled plate whose BORE WALL is missing: two rims, and a trap for a hole-filler.

    THE PART-RUINING CASE, as a fixture. A 100x100x10 plate with an 8 mm through-hole, exported
    without the cylindrical face, leaves two planar circular loops of identical span - each one
    indistinguishable from a small fillable hole. Patching both seals the bore and hands the
    customer back a plate with no bolt hole, which is a ruined part rather than a repaired one.

    It exists so that the discriminator protecting against exactly that is tested on the real
    geometry rather than on a description of it.

    `profile="hex"` cuts an 8 mm HEXAGONAL through-hole instead and drops its six flat walls: the
    reviewer's reproduction of a fill sealing a non-round passage end to end, which a roundness
    test let through. The cross-section of a through-hole does not change what sealing it does.
    """
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.GeomAbs import GeomAbs_Cylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    plate = BRepPrimAPI_MakeBox(100.0, 100.0, 10.0).Shape()
    if profile == "hex":
        import math

        from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
        from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
        from OCP.gp import gp_Vec

        polygon = BRepBuilderAPI_MakePolygon()
        for k in range(6):
            a = 2 * math.pi * k / 6
            polygon.Add(gp_Pnt(50 + 8 * math.cos(a), 50 + 8 * math.sin(a), -1))
        polygon.Close()
        bore = BRepPrimAPI_MakePrism(
            BRepBuilderAPI_MakeFace(polygon.Wire()).Face(), gp_Vec(0, 0, 12)).Shape()
    else:
        bore = BRepPrimAPI_MakeCylinder(
            gp_Ax2(gp_Pnt(50, 50, -1), gp_Dir(0, 0, 1)), 8.0, 12.0).Shape()
    drilled = BRepAlgoAPI_Cut(plate, bore).Shape()

    def _is_bore_wall(face) -> bool:
        if profile != "hex":
            return BRepAdaptor_Surface(face).GetType() == GeomAbs_Cylinder
        from OCP.BRepGProp import BRepGProp
        from OCP.GProp import GProp_GProps

        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        c = props.CentreOfMass()
        return abs(c.X() - 50) < 20 and abs(c.Y() - 50) < 20 and 0 < c.Z() < 10

    keep = []
    explorer = TopExp_Explorer(drilled, TopAbs_FACE)
    while explorer.More():
        face = TopoDS.Face_s(explorer.Current())
        if not _is_bore_wall(face):
            keep.append(face)          # every face except the bore wall
        explorer.Next()

    sewing = BRepBuilderAPI_Sewing(1e-6)
    for face in keep:
        sewing.Add(face)
    sewing.Perform()

    writer = STEPControl_Writer()
    if not Interface_Static.SetCVal_s("write.step.unit", unit):
        raise RuntimeError(
            f"OCC refused write.step.unit={unit!r}; the STEP controller is not initialised, so "
            "this fixture would silently emit millimetres instead.")
    writer.Transfer(sewing.SewedShape(), STEPControl_AsIs)
    path.parent.mkdir(parents=True, exist_ok=True)
    writer.Write(str(path))
    return path


def write_missing_passage_wall_step(path: Path, kind: str, unit: str = "MM",
                                   reverse: bool = False) -> Path:
    """A plate with ONE THROUGH-PASSAGE whose walls are missing, in any of the shapes that sealed.

    The adversarial re-review's builder, as a fixture: cut the passage with a tool, then drop
    EVERY face the tool contributed (read from the boolean's own history, not guessed from
    geometry), sew the rest. What is left is two rims and nothing joining them.

    `kind`: `round`, `hex`, `slot`, `square`, `pent`, `tri` (an 8 mm-class passage straight through
    a 100x100x10 plate); `angled<deg>` (a 4 mm-radius round hole tilted that many degrees from the
    plate normal - its exit rim sits thickness x tan(deg) to the side); `wedge<deg>` (a straight
    hole through a plate whose top face is tilted that many degrees - the two rims not parallel).
    Every one of the angled and wedge cases was delivered "Repaired within the conservative caps"
    with the hole sealed while the guard demanded parallel rims on one normal.

    `dbar<offset>` (a D-section bar - flat top, round bottom) and `flipbar<offset>` (round top,
    flat bottom) take a 4 mm-radius vertical hole `offset` mm off the bar's axis, so ONE RIM IS A
    SADDLE on the curved side and the other is flat. `reverse=True` sews the faces in the opposite
    order: the same part, and on that order the flat rim's fill was KEPT and the hole sealed, while
    the original order was saved only by the stall limit stopping the run first.
    """
    import math

    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepBuilderAPI import (
        BRepBuilderAPI_MakeEdge,
        BRepBuilderAPI_MakeFace,
        BRepBuilderAPI_MakePolygon,
        BRepBuilderAPI_MakeWire,
        BRepBuilderAPI_Sewing,
        BRepBuilderAPI_Transform,
    )
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakePrism
    from OCP.GC import GC_MakeArcOfCircle
    from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp, TopExp_Explorer
    from OCP.TopTools import TopTools_IndexedMapOfShape

    def prism(wire, height=40.0):
        return BRepPrimAPI_MakePrism(BRepBuilderAPI_MakeFace(wire).Face(),
                                     gp_Vec(0, 0, height)).Shape()

    def polygon(sides, radius, z=-15.0):
        made = BRepBuilderAPI_MakePolygon()
        for k in range(sides):
            a = 2 * math.pi * k / sides + 0.3
            made.Add(gp_Pnt(50 + radius * math.cos(a), 50 + radius * math.sin(a), z))
        made.Close()
        return made.Wire()

    def slot(z=-15.0, length=14.0, radius=3.0):
        h = length / 2
        def at(x, y):
            return gp_Pnt(50 + x, 50 + y, z)
        wire = BRepBuilderAPI_MakeWire()
        wire.Add(BRepBuilderAPI_MakeEdge(at(-h, -radius), at(h, -radius)).Edge())
        wire.Add(BRepBuilderAPI_MakeEdge(GC_MakeArcOfCircle(
            at(h, -radius), at(h + radius, 0), at(h, radius)).Value()).Edge())
        wire.Add(BRepBuilderAPI_MakeEdge(at(h, radius), at(-h, radius)).Edge())
        wire.Add(BRepBuilderAPI_MakeEdge(GC_MakeArcOfCircle(
            at(-h, radius), at(-h - radius, 0), at(-h, -radius)).Value()).Edge())
        return wire.Wire()

    def cylinder(radius, height=40.0, at=(50, 50, -15)):
        return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*at), gp_Dir(0, 0, 1)), radius,
                                        height).Shape()

    def rotated(shape, degrees, about=(50, 50, 5)):
        turn = gp_Trsf()
        turn.SetRotation(gp_Ax1(gp_Pnt(*about), gp_Dir(1, 0, 0)), math.radians(degrees))
        return BRepBuilderAPI_Transform(shape, turn, True).Shape()

    plate = BRepPrimAPI_MakeBox(100.0, 100.0, 10.0).Shape()
    if kind == "round":
        tool = cylinder(8.0)
    elif kind in ("hex", "square", "pent", "tri"):
        sides, radius = {"hex": (6, 8.0), "square": (4, 8.0), "pent": (5, 7.0), "tri": (3, 9.0)}[kind]
        tool = prism(polygon(sides, radius))
    elif kind == "slot":
        tool = prism(slot())
    elif kind.startswith("angled"):
        tool = rotated(cylinder(4.0, 80.0, at=(50, 50, -35)), float(kind[len("angled"):]))
    elif kind.startswith(("dbar", "flipbar")):
        bar = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 50, 30), gp_Dir(1, 0, 0)), 30, 100).Shape()
        flat_top = kind.startswith("dbar")
        offset = float(kind[len("dbar" if flat_top else "flipbar"):])
        trim = BRepPrimAPI_MakeBox(gp_Pnt(-1, 0, 50 if flat_top else -10), 102, 100,
                                   40 if flat_top else 20).Shape()
        plate = BRepAlgoAPI_Cut(bar, trim).Shape()
        tool = cylinder(4.0, 80.0, at=(50, 50 + offset, -10))
    elif kind.startswith("wedge"):
        slab = BRepPrimAPI_MakeBox(gp_Pnt(-50, -50, 10.0), 200, 200, 60).Shape()
        cutter = rotated(slab, float(kind[len("wedge"):]), about=(50, 50, 10.0))
        plate = BRepAlgoAPI_Cut(BRepPrimAPI_MakeBox(100.0, 100.0, 30.0).Shape(), cutter).Shape()
        tool = cylinder(4.0)
    else:
        raise ValueError(f"unknown passage kind {kind!r}")

    cut = BRepAlgoAPI_Cut(plate, tool)
    walls = TopTools_IndexedMapOfShape()
    tool_faces = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(tool, TopAbs_FACE, tool_faces)
    for i in range(1, tool_faces.Extent() + 1):
        face = tool_faces.FindKey(i)
        modified = cut.Modified(face)
        for piece in modified:
            walls.Add(piece)
        if not cut.IsDeleted(face) and modified.Size() == 0:
            walls.Add(face)

    keep = []
    explorer = TopExp_Explorer(cut.Shape(), TopAbs_FACE)
    while explorer.More():
        if not walls.Contains(explorer.Current()):
            keep.append(explorer.Current())   # every face except the passage's walls
        explorer.Next()
    sewing = BRepBuilderAPI_Sewing(1e-6)
    for face in (reversed(keep) if reverse else keep):
        sewing.Add(face)
    sewing.Perform()

    writer = STEPControl_Writer()
    if not Interface_Static.SetCVal_s("write.step.unit", unit):
        raise RuntimeError(f"OCC refused write.step.unit={unit!r}")
    writer.Transfer(sewing.SewedShape(), STEPControl_AsIs)
    path.parent.mkdir(parents=True, exist_ok=True)
    writer.Write(str(path))
    return path


def write_mixed_assembly_step(path: Path, unit: str = "MM", side: float = SIDE) -> Path:
    """One sound SOLID body beside a body that is only loose faces, in one STEP assembly.

    THE CASE A WHOLE-SHAPE JUDGEMENT GETS WRONG. Sewing an assembly that already contains a solid
    hands back a shell and the solid is gone, so the repair must not sew the assembly. But the
    second body here is what sewing exists for: six faces at the same place as a box, never joined
    into a shell, which is how surface exports and translated assemblies routinely arrive. Judge
    the assembly as one shape and you must either destroy the solid or leave the loose body loose.

    Real, not contrived: a fabricated part and a bought-in surface model in the same STEP is an
    ordinary customer upload.
    """
    from OCP.BRep import BRep_Builder
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS, TopoDS_Compound

    solid = BRepPrimAPI_MakeBox(side, side, side).Shape()
    # the second body, offset so the two do not touch: its faces are added INDIVIDUALLY, so the
    # body has no shell and no solid - exactly what sewing is for
    loose = BRepPrimAPI_MakeBox(gp_Pnt(side * 2, 0.0, 0.0),
                                gp_Pnt(side * 3, side, side)).Shape()

    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    builder.Add(compound, solid)

    loose_body = TopoDS_Compound()
    builder.MakeCompound(loose_body)
    explorer = TopExp_Explorer(loose, TopAbs_FACE)
    while explorer.More():
        builder.Add(loose_body, TopoDS.Face_s(explorer.Current()))
        explorer.Next()
    builder.Add(compound, loose_body)

    writer = STEPControl_Writer()
    if not Interface_Static.SetCVal_s("write.step.unit", unit):
        raise RuntimeError(
            f"OCC refused write.step.unit={unit!r}; the STEP controller is not initialised, so "
            "this fixture would silently emit millimetres instead.")
    writer.Transfer(compound, STEPControl_AsIs)
    path.parent.mkdir(parents=True, exist_ok=True)
    writer.Write(str(path))
    return path


def _write_step(shape, path: Path, unit: str) -> Path:
    """Write one shape, refusing rather than silently emitting the wrong unit."""
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    writer = STEPControl_Writer()
    if not Interface_Static.SetCVal_s("write.step.unit", unit):
        raise RuntimeError(
            f"OCC refused write.step.unit={unit!r}; the STEP controller is not initialised, so "
            "this fixture would silently emit millimetres instead.")
    writer.Transfer(shape, STEPControl_AsIs)
    path.parent.mkdir(parents=True, exist_ok=True)
    writer.Write(str(path))
    return path


def write_small_edge_step(path: Path, unit: str = "MM") -> Path:
    """A plate with a SLIVER EDGE: a long thin notch cut into the y=0 face, ending in a 0.002 mm tip.

    WHAT A SMALL EDGE ACTUALLY IS AND WHY IT IS NOT CONTRIVED. A mesher sizes its cells off the
    local feature size, so an edge 0.002 mm long on a 100 mm plate forces cells five orders of
    magnitude smaller than the part - which is how a mesh job runs for hours and then runs out of
    memory. They arrive from tangent-continuous fillets, from translation round-off, and from
    booleans that just miss: this one is built by cutting a very thin wedge off a corner, which is
    the boolean case. The notch runs from x=0 to x=20 and narrows to 0.002 mm, so its end face is a
    sliver 0.002 mm wide whose top and bottom edges (at x=20) are each 0.002 mm long.

    `localize_scan` reports those two edges as `small_edge` (and the sliver face as `small_face`) because its length is under
    SMALL_EDGE_LENGTH_FRACTION (1e-4) of the part's diagonal. The plate's diagonal is about 141 mm,
    so the threshold is about 0.014 mm and the sliver below is well under it.
    """
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakePrism
    from OCP.gp import gp_Pnt, gp_Vec

    plate = BRepPrimAPI_MakeBox(100.0, 100.0, 10.0).Shape()
    # A wedge whose tip is 0.002 mm wide: cutting it leaves an edge of that length at the tip.
    polygon = BRepBuilderAPI_MakePolygon()
    for point in (gp_Pnt(0.0, -1.0, -1.0), gp_Pnt(20.0, -1.0, -1.0),
                  gp_Pnt(20.0, 0.002, -1.0), gp_Pnt(0.0, 0.0, -1.0)):
        polygon.Add(point)
    polygon.Close()
    wedge = BRepPrimAPI_MakePrism(
        BRepBuilderAPI_MakeFace(polygon.Wire(), True).Face(), gp_Vec(0.0, 0.0, 12.0)).Shape()
    return _write_step(BRepAlgoAPI_Cut(plate, wedge).Shape(), path, unit)


def write_loose_faces_step(path: Path, unit: str = "MM") -> Path:
    """SIX BOX FACES AT THE RIGHT PLACES, NEVER JOINED - no solid, no joined shell, nothing sewn.

    HOW THIS DIFFERS FROM THE OPEN SHELL AND THE MIXED ASSEMBLY, both of which already exist.
    `write_open_shell_step` is a sewn shell declared a solid with one face MISSING: the defect is
    a hole. `write_mixed_assembly_step` puts a loose body BESIDE a sound solid: the defect is that
    a whole-shape judgement must not be made. This one is a single body that is complete but
    unjoined: every face is present, the geometry closes, and the topology says nothing connects.

    It is the commonest way a surface export arrives, and it is what sewing exists for - so it is
    the fixture a targeted `sew_faces` has to be proved on. The repair must END with a shell, and
    `_as_solid_if_closed` should then promote it.

    WHAT THE KERNEL AND THE LOCALISER SAY ABOUT IT. After a STEP round trip `measure` reports 0
    solids and SIX shells (the reader wraps each loose face in a shell of its own, so "unjoined"
    reads as one shell per face, and sewing makes the shell count FALL from 6 to 1), and `localize_scan` reports six
    `open_shell` defects from ShapeAnalysis_FreeBounds, each a closed free boundary of four edges:
    every edge of the box is unshared, so every one is a free boundary. That is the real signature
    of an unsewn surface export, not a face missing.
    """
    from OCP.BRep import BRep_Builder
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS, TopoDS_Compound

    box = BRepPrimAPI_MakeBox(SIDE, SIDE, SIDE).Shape()
    builder = BRep_Builder()
    loose = TopoDS_Compound()
    builder.MakeCompound(loose)
    explorer = TopExp_Explorer(box, TopAbs_FACE)
    while explorer.More():
        # EACH FACE ADDED TO A COMPOUND INDIVIDUALLY. Adding the box itself would carry its shell
        # and its solid along, which is the opposite of this fixture.
        builder.Add(loose, TopoDS.Face_s(explorer.Current()))
        explorer.Next()
    return _write_step(loose, path, unit)
