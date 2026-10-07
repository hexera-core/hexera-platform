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


def write_missing_bore_wall_step(path: Path, unit: str = "MM") -> Path:
    """A drilled plate whose BORE WALL is missing: two rims, and a trap for a hole-filler.

    THE PART-RUINING CASE, as a fixture. A 100x100x10 plate with an 8 mm through-hole, exported
    without the cylindrical face, leaves two planar circular loops of identical span - each one
    indistinguishable from a small fillable hole. Patching both seals the bore and hands the
    customer back a plate with no bolt hole, which is a ruined part rather than a repaired one.

    It exists so that the discriminator protecting against exactly that is tested on the real
    geometry rather than on a description of it.
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
    bore = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(50, 50, -1), gp_Dir(0, 0, 1)), 8.0, 12.0).Shape()
    drilled = BRepAlgoAPI_Cut(plate, bore).Shape()

    keep = []
    explorer = TopExp_Explorer(drilled, TopAbs_FACE)
    while explorer.More():
        face = TopoDS.Face_s(explorer.Current())
        if BRepAdaptor_Surface(face).GetType() != GeomAbs_Cylinder:
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
