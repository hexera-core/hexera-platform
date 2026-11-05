# Responsibility: Bring an exact CAD format that is not STEP or IGES to the canonical STEP every CAD consumer reads.
# Owns: the OpenCASCADE BREP read and the STEP write that carries its coordinates unchanged.
# Boundaries: no scaling, no healing: the STEP holds exactly the shape the file held, in the file's own numbers.
# Collaborates with: cad/ingest/canonical.py; contracts/coordinate_state.py (why the millimetre label is exact).
from __future__ import annotations

from pathlib import Path


class CadReadError(ValueError):
    pass


def read_brep(path: Path):
    """An OpenCASCADE shape from a .brep file, text or binary."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape

    shape = TopoDS_Shape()
    try:
        BRepTools.Read_s(shape, str(path), BRep_Builder())
    except Exception:  # noqa: BLE001 - the binary reader is tried next
        shape = TopoDS_Shape()
    if shape.IsNull():
        try:
            from OCP.BinTools import BinTools

            shape = TopoDS_Shape()
            BinTools.Read_s(shape, str(path))
        except Exception:  # noqa: BLE001
            shape = TopoDS_Shape()
    if shape.IsNull():
        raise CadReadError("the BREP file could not be read by OpenCASCADE")
    return shape


def write_step(shape, dest: Path) -> None:
    """The shape as STEP, its coordinates written unchanged under a millimetre label.

    The label is OpenCASCADE's own system unit, so reading the file back applies no conversion,
    and parser_applied_unit reports exactly that: the staging seam then scales by the CONFIRMED
    unit alone (interpretation / applied * OCC output), which is right whatever the BREP was drawn
    in. A BREP states no unit, so the upload never records one from this label - it asks."""
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    Interface_Static.SetCVal_s("write.step.unit", "MM")
    writer = STEPControl_Writer()
    if writer.Transfer(shape, STEPControl_AsIs) != IFSelect_RetDone:
        raise CadReadError("the shape could not be written as STEP")
    if writer.Write(str(dest)) != IFSelect_RetDone:
        raise CadReadError("the STEP file could not be written")


def brep_to_step(src: Path, dest: Path) -> dict:
    shape = read_brep(src)
    write_step(shape, dest)
    return shape_stats(shape)


def shape_stats(shape) -> dict:
    """Solids, faces, volume, area and box: what a BREP -> STEP round trip is compared on."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer

    def count(kind) -> int:
        exp, n = TopExp_Explorer(shape, kind), 0
        while exp.More():
            n += 1
            exp.Next()
        return n

    vol, area = GProp_GProps(), GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, vol)
    BRepGProp.SurfaceProperties_s(shape, area)
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    return {"solids": count(TopAbs_SOLID), "faces": count(TopAbs_FACE),
            "volume": abs(float(vol.Mass())), "area": float(area.Mass()),
            "bounds_min": [x0, y0, z0], "bounds_max": [x1, y1, z1]}


def read_step(path: Path):
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPControl import STEPControl_Reader

    reader = STEPControl_Reader()
    if reader.ReadFile(str(path)) != IFSelect_RetDone:
        raise CadReadError("the STEP file could not be read")
    reader.TransferRoots()
    return reader.OneShape()
