# Responsibility: One real part with small faces that a `fill_loop` can honestly put back, for the v4 native tests.
# Boundaries: needs OCP (native tier). A 100 x 60 x 40 mm box whose faces are each far larger than 5% of the diagonal,
#             with three 3 x 3 mm squares imprinted FLUSH in its faces (a split, so the box is still one solid and the
#             squares are coplanar with their surroundings). Deleting a square leaves a 4-edge loop spanning ~3.4% of the
#             diagonal that a plane closes with zero deviation: the realistic "small face lost in translation".
#             Deleting any other face leaves a loop spanning 58-87%, which the op refuses by design.
from __future__ import annotations

from pathlib import Path

from OCP.BRepAlgoAPI import BRepAlgoAPI_Splitter
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.gp import gp_Pnt
from OCP.IFSelect import IFSelect_RetDone
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
from OCP.TopTools import TopTools_ListOfShape

SIZE = (100.0, 60.0, 40.0)
HALF = 1.5


def _square(cx: float, cy: float, cz: float, normal: str, half: float = HALF):
    def point(u: float, v: float) -> gp_Pnt:
        return {"z": gp_Pnt(cx + u, cy + v, cz), "x": gp_Pnt(cx, cy + u, cz + v)}[normal]

    polygon = BRepBuilderAPI_MakePolygon()
    for u, v in ((-half, -half), (half, -half), (half, half), (-half, half)):
        polygon.Add(point(u, v))
    polygon.Close()
    return BRepBuilderAPI_MakeFace(polygon.Wire()).Face()


def imprint_squares(shape, squares):
    """Split `shape` by flush squares, each `(cx, cy, cz, normal, half)`: the part stays one solid and gains one
    small face per square, coplanar with the face it was cut from."""
    splitter = BRepAlgoAPI_Splitter()
    arguments, tools = TopTools_ListOfShape(), TopTools_ListOfShape()
    arguments.Append(shape)
    for cx, cy, cz, normal, half in squares:
        tools.Append(_square(cx, cy, cz, normal, half))
    splitter.SetArguments(arguments)
    splitter.SetTools(tools)
    splitter.Build()
    assert splitter.IsDone(), "the flush squares could not be imprinted"
    return splitter.Shape()


def flush_patch_box():
    return imprint_squares(BRepPrimAPI_MakeBox(*SIZE).Shape(),
                           [(30, 20, 40, "z", HALF), (70, 40, 0, "z", HALF), (0, 30, 20, "x", HALF)])


def chamfer_corner(shape, leg: float):
    """`shape` (a box with a corner at the origin) with that corner cut off by a plane through (leg,0,0), (0,leg,0),
    (0,0,leg): one small INCLINED triangular face. A planar fill of it is not coplanar with any face around it, which is
    what `fill_loop`'s faithful check refuses."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.gp import gp_Vec

    polygon = BRepBuilderAPI_MakePolygon()
    for point in (gp_Pnt(leg, 0, 0), gp_Pnt(0, leg, 0), gp_Pnt(0, 0, leg)):
        polygon.Add(point)
    polygon.Close()
    tool = BRepPrimAPI_MakePrism(BRepBuilderAPI_MakeFace(polygon.Wire()).Face(), gp_Vec(-leg, -leg, -leg)).Shape()
    cut = BRepAlgoAPI_Cut(shape, tool)
    assert cut.IsDone(), "the corner could not be chamfered"
    return cut.Shape()


def chamfered_flush_patch_box(leg: float = 3.0):
    """The flush-patch box plus one inclined corner face: it holds BOTH kinds of small face, the one a plane puts back
    and the one it cannot."""
    return chamfer_corner(flush_patch_box(), leg)


def write_chamfered_flush_patch_box(path: Path) -> Path:
    return write_step(chamfered_flush_patch_box(), path)


def write_step(shape, path: Path) -> Path:
    writer = STEPControl_Writer()
    writer.Transfer(shape, STEPControl_AsIs)
    assert writer.Write(str(path)) == IFSelect_RetDone
    return path


def write_flush_patch_box(path: Path) -> Path:
    return write_step(flush_patch_box(), path)


def three_hole_box():
    """The flush-patch box with ALL THREE small squares deleted: one solid's worth of faces, three separate free
    loops (each ~3.4% of the diagonal, each a plane closes with zero deviation). The repair needs three `fill_loop`s
    and then one `make_volume`."""
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepTools import BRepTools_ReShape
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    shape = flush_patch_box()
    reshape = BRepTools_ReShape()
    removed = 0
    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    while explorer.More():
        face = TopoDS.Face_s(explorer.Current())
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        if abs(props.Mass() - (2 * HALF) ** 2) < 1e-6:
            reshape.Remove(face)
            removed += 1
        explorer.Next()
    assert removed == 3, f"expected to delete the three flush squares, found {removed}"
    return reshape.Apply(shape)


def write_three_hole_box(path: Path) -> Path:
    return write_step(three_hole_box(), path)
