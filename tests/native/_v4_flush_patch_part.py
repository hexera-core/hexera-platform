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


def _square(cx: float, cy: float, cz: float, normal: str):
    def point(u: float, v: float) -> gp_Pnt:
        return {"z": gp_Pnt(cx + u, cy + v, cz), "x": gp_Pnt(cx, cy + u, cz + v)}[normal]

    polygon = BRepBuilderAPI_MakePolygon()
    for u, v in ((-HALF, -HALF), (HALF, -HALF), (HALF, HALF), (-HALF, HALF)):
        polygon.Add(point(u, v))
    polygon.Close()
    return BRepBuilderAPI_MakeFace(polygon.Wire()).Face()


def flush_patch_box():
    box = BRepPrimAPI_MakeBox(*SIZE).Shape()
    splitter = BRepAlgoAPI_Splitter()
    arguments, tools = TopTools_ListOfShape(), TopTools_ListOfShape()
    arguments.Append(box)
    for tool in (_square(30, 20, 40, "z"), _square(70, 40, 0, "z"), _square(0, 30, 20, "x")):
        tools.Append(tool)
    splitter.SetArguments(arguments)
    splitter.SetTools(tools)
    splitter.Build()
    assert splitter.IsDone(), "the flush squares could not be imprinted"
    return splitter.Shape()


def write_step(shape, path: Path) -> Path:
    writer = STEPControl_Writer()
    writer.Transfer(shape, STEPControl_AsIs)
    assert writer.Write(str(path)) == IFSelect_RetDone
    return path


def write_flush_patch_box(path: Path) -> Path:
    return write_step(flush_patch_box(), path)
