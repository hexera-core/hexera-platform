# Responsibility: Read and write STEP through XCAF so body/face names, colours and assembly
#                 structure survive, with the shape itself held as a BREP file.
# Owns: XcafDoc, the stable label ids, `read`/`write`, and the `read_step_xcaf`/`write_step_xcaf`
#       kernel calls.
# Boundaries: `read` and `write` call OCCT, so they run inside a kernel worker only (the parent
#             reaches them through `WorkerPool.call`). OCP is imported inside functions so this
#             module imports on a host without it. PMI is carried where the STEP reader and writer
#             carry it; nothing here checks it.
"""XCAF STEP I/O: the shape as a BREP file plus a label map keyed by stable ids.

Stable ids are structural, so they survive a write and re-read: a free shape is "0", "1", ...; the
component j of an assembly node is "<id>/<j>"; a component's referred part is "<id>@"; a named or
coloured sub-shape is "<id>/F<k>" or "<id>/S<k>", k being its index in the owning shape's face or
solid map. The label map is `{stable_id: {"name": str|None, "color": [r,g,b]|None, "parent": id|None}}`.

The BREP is always in millimetres: OCCT converts on read and the writer converts back. The file's own
unit is carried separately in `XcafDoc.units`, and `write` restores it, so a unit is never lost.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field

from meshpipeline.cad.repair_v4.kernel_calls import register
from meshpipeline.cad.repair_v4.units import WRITER_CODES, canonical_unit


@dataclass
class XcafDoc:
    shape_brep: str
    labels: dict[str, dict]
    units: str = "mm"
    assembly_ids: list[str] = field(default_factory=list)


def _set_static(name: str, value: int | str) -> None:
    """Set a STEP parameter and prove it took: OCCT ignores a parameter set before a reader or writer exists."""
    from OCP.Interface import Interface_Static

    took = (
        Interface_Static.SetIVal_s(name, value) if isinstance(value, int) else Interface_Static.SetCVal_s(name, value)
    )
    if not took:
        raise RuntimeError(f"could not set STEP parameter {name} to {value!r}")


def _new_document():
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDocStd import TDocStd_Document
    from OCP.XCAFApp import XCAFApp_Application

    doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    XCAFApp_Application.GetApplication_s().InitDocument(doc)
    return doc


def _name_of(label) -> str | None:
    from OCP.TDataStd import TDataStd_Name

    # FindAttribute on a label that has no name segfaults the 7.9 binding, so ask first.
    if not label.IsAttribute(TDataStd_Name.GetID_s()):
        return None
    attr = TDataStd_Name()
    if label.FindAttribute(TDataStd_Name.GetID_s(), attr):
        return attr.Get().ToExtString() or None
    return None


def _set_name(label, name: str | None) -> None:
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name

    if name:
        TDataStd_Name.Set_s(label, TCollection_ExtendedString(name))


def _color_of(label) -> list[float] | None:
    from OCP.Quantity import Quantity_Color
    from OCP.XCAFDoc import XCAFDoc_ColorGen, XCAFDoc_ColorSurf, XCAFDoc_ColorTool

    color = Quantity_Color()
    for kind in (XCAFDoc_ColorSurf, XCAFDoc_ColorGen):
        if XCAFDoc_ColorTool.GetColor_s(label, kind, color):
            return [color.Red(), color.Green(), color.Blue()]
    return None


def _set_color(color_tool, label, rgb: list[float] | None) -> None:
    from OCP.Quantity import Quantity_Color, Quantity_TOC_RGB
    from OCP.XCAFDoc import XCAFDoc_ColorSurf

    if rgb is not None:
        color_tool.SetColor(label, Quantity_Color(rgb[0], rgb[1], rgb[2], Quantity_TOC_RGB), XCAFDoc_ColorSurf)


def _subshape_kinds():
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID

    return (("F", TopAbs_FACE), ("S", TopAbs_SOLID))


def _subshape_index(owner_shape, shape, kind):
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    shapes = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(owner_shape, kind, shapes)
    return shapes.FindIndex(shape)  # 0 when absent


def _collect(shape_tool, label, node_id: str, parent: str | None, labels: dict) -> None:
    """Record `label` and everything under it. A component and its referred part are two nodes."""
    from OCP.TDF import TDF_Label, TDF_LabelSequence

    labels[node_id] = {"name": _name_of(label), "color": _color_of(label), "parent": parent}
    if shape_tool.IsReference_s(label):
        referred = TDF_Label()
        shape_tool.GetReferredShape_s(label, referred)
        _collect(shape_tool, referred, f"{node_id}@", node_id, labels)
        return
    if shape_tool.IsAssembly_s(label):
        components = TDF_LabelSequence()
        shape_tool.GetComponents_s(label, components)
        for j in range(1, components.Length() + 1):
            _collect(shape_tool, components.Value(j), f"{node_id}/{j - 1}", node_id, labels)
        return
    owner = shape_tool.GetShape_s(label)
    subs = TDF_LabelSequence()
    shape_tool.GetSubShapes_s(label, subs)
    for j in range(1, subs.Length() + 1):
        sub = subs.Value(j)
        name, color = _name_of(sub), _color_of(sub)
        if name is None and color is None:
            continue
        shape = shape_tool.GetShape_s(sub)
        for letter, kind in _subshape_kinds():
            if shape.ShapeType() == kind:
                index = _subshape_index(owner, shape, kind)
                if index:
                    labels[f"{node_id}/{letter}{index}"] = {"name": name, "color": color, "parent": node_id}


def _file_units(reader) -> str:
    from OCP.TColStd import TColStd_SequenceOfAsciiString

    lengths = TColStd_SequenceOfAsciiString()
    reader.Reader().FileUnits(lengths, TColStd_SequenceOfAsciiString(), TColStd_SequenceOfAsciiString())
    if lengths.Length() == 0:
        return "mm"  # a file with no unit context is read as millimetres by OCCT
    return canonical_unit(lengths.Value(1).ToCString())


def read(path: str, brep_out: str | None = None) -> XcafDoc:
    """Read a STEP file through XCAF. Runs in a kernel worker."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.Interface import Interface_Static
    from OCP.STEPCAFControl import STEPCAFControl_Reader
    from OCP.TDF import TDF_LabelSequence
    from OCP.TopoDS import TopoDS_Compound
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    reader = STEPCAFControl_Reader()
    # STEP parameters are registered, and reset to their defaults, by constructing the reader, so
    # they are set AFTER it exists and their return value is checked: set earlier they are ignored.
    _set_static("read.stepcaf.subshapes.name", 1)  # default 0 drops face names
    _set_static("xstep.cascade.unit", "MM")  # the BREP is millimetres whatever unit the file has
    if Interface_Static.CVal_s("xstep.cascade.unit") != "MM":
        raise RuntimeError("STEP reader is not converting to millimetres")
    for setter in (
        reader.SetNameMode,
        reader.SetColorMode,
        reader.SetLayerMode,
        reader.SetPropsMode,
        reader.SetGDTMode,
    ):
        setter(True)
    if reader.ReadFile(path) != IFSelect_RetDone:
        raise ValueError(f"STEP file could not be read: {path}")
    doc = _new_document()
    if not reader.Transfer(doc):
        raise ValueError(f"STEP file could not be transferred: {path}")
    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    free = TDF_LabelSequence()
    shape_tool.GetFreeShapes(free)

    root = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(root)
    labels: dict[str, dict] = {}
    assembly_ids: list[str] = []
    for i in range(1, free.Length() + 1):
        label = free.Value(i)
        builder.Add(root, shape_tool.GetShape_s(label))
        node_id = str(i - 1)
        _collect(shape_tool, label, node_id, None, labels)
        if shape_tool.IsAssembly_s(label):
            assembly_ids.append(node_id)

    if brep_out is None:
        brep_out = os.path.join(tempfile.mkdtemp(prefix="xcaf_"), "shape.brep")
    if not BRepTools.Write_s(root, brep_out):
        raise ValueError(f"could not write BREP to {brep_out}")
    return XcafDoc(shape_brep=brep_out, labels=labels, units=_file_units(reader), assembly_ids=assembly_ids)


def _apply(shape_tool, color_tool, label, node_id: str, labels: dict) -> None:
    """Copy names and colours onto a freshly added label tree, by the same ids `_collect` made."""
    from OCP.TDF import TDF_Label, TDF_LabelSequence

    entry = labels.get(node_id)
    if entry is not None:
        _set_name(label, entry.get("name"))
        _set_color(color_tool, label, entry.get("color"))
    if shape_tool.IsReference_s(label):
        referred = TDF_Label()
        shape_tool.GetReferredShape_s(label, referred)
        _apply(shape_tool, color_tool, referred, f"{node_id}@", labels)
        return
    if shape_tool.IsAssembly_s(label):
        components = TDF_LabelSequence()
        shape_tool.GetComponents_s(label, components)
        for j in range(1, components.Length() + 1):
            _apply(shape_tool, color_tool, components.Value(j), f"{node_id}/{j - 1}", labels)
        return
    owner = shape_tool.GetShape_s(label)
    for letter, kind in _subshape_kinds():
        prefix = f"{node_id}/{letter}"
        for sub_id, sub_entry in labels.items():
            if not sub_id.startswith(prefix) or not sub_id[len(prefix) :].isdigit():
                continue
            from OCP.TopExp import TopExp
            from OCP.TopTools import TopTools_IndexedMapOfShape

            shapes = TopTools_IndexedMapOfShape()
            TopExp.MapShapes_s(owner, kind, shapes)
            index = int(sub_id[len(prefix) :])
            if index > shapes.Extent():
                continue
            sub_label = shape_tool.AddSubShape(label, shapes.FindKey(index))
            _set_name(sub_label, sub_entry.get("name"))
            _set_color(color_tool, sub_label, sub_entry.get("color"))


def write(doc: XcafDoc, path: str) -> None:
    """Write `doc` as STEP in its own unit, with names, colours and assembly structure. Kernel only."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.Interface import Interface_Static
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.STEPControl import STEPControl_AsIs
    from OCP.TopoDS import TopoDS_Iterator, TopoDS_Shape
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    if doc.units not in WRITER_CODES:
        raise ValueError(f"cannot write unit {doc.units!r}; known: {sorted(WRITER_CODES)}")
    root = TopoDS_Shape()
    if not BRepTools.Read_s(root, doc.shape_brep, BRep_Builder()):
        raise ValueError(f"could not read BREP {doc.shape_brep}")

    xdoc = _new_document()
    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(xdoc.Main())
    color_tool = XCAFDoc_DocumentTool.ColorTool_s(xdoc.Main())
    children = TopoDS_Iterator(root)
    index = 0
    while children.More():
        node_id = str(index)
        label = shape_tool.AddShape(children.Value(), node_id in doc.assembly_ids)
        _apply(shape_tool, color_tool, label, node_id, doc.labels)
        children.Next()
        index += 1

    # The writer must exist first: STEP parameters are registered by constructing one, and setting
    # `write.step.unit` before that is a silent no-op that writes the file in millimetres.
    writer = STEPCAFControl_Writer()
    previous = Interface_Static.CVal_s("write.step.unit")
    _set_static("write.step.unit", WRITER_CODES[doc.units])
    _set_static("write.stepcaf.subshapes.name", 1)  # default 0 writes ADVANCED_FACE('') for every face
    try:
        for setter in (
            writer.SetNameMode,
            writer.SetColorMode,
            writer.SetLayerMode,
            writer.SetPropsMode,
            writer.SetDimTolMode,
        ):
            setter(True)
        if not writer.Transfer(xdoc, STEPControl_AsIs):
            raise ValueError("STEP transfer failed")
        if writer.Write(path) != IFSelect_RetDone:
            raise ValueError(f"STEP write failed: {path}")
    finally:
        Interface_Static.SetCVal_s("write.step.unit", previous)


@register("read_step_xcaf")
def _read_step_xcaf(payload: dict) -> dict:
    doc = read(payload["path"], payload.get("brep_out"))
    return {
        "brep": doc.shape_brep,
        "labels": doc.labels,
        "units": doc.units,
        "assembly_ids": doc.assembly_ids,
    }


@register("write_step_xcaf")
def _write_step_xcaf(payload: dict) -> dict:
    write(
        XcafDoc(
            shape_brep=payload["brep"],
            labels=payload["labels"],
            units=payload.get("units", "mm"),
            assembly_ids=payload.get("assembly_ids", []),
        ),
        payload["path"],
    )
    return {"path": payload["path"]}
