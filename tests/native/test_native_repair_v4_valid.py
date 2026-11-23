# Responsibility: Verify layer 1 of the v4 checker (valid) and the XCAF round trip against the real
#                 kernel, on fixtures built here: conformal contact is not overlap, inch STEP keeps
#                 its unit, a surface-only file has no solids, names survive read -> write -> read.
# Boundaries: the native tier - the only place OpenCASCADE exists. The kernel under test runs in a
#             WorkerPool process; this module touches OCP only to author fixtures.
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("OCP.XCAFDoc", reason="the CAD kernel is a native-tier dependency")

from OCP.BRep import BRep_Builder
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakePolygon,
    BRepBuilderAPI_MakeSolid,
    BRepBuilderAPI_Sewing,
)
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.BRepTools import BRepTools
from OCP.gp import gp_Pln, gp_Pnt
from OCP.IFSelect import IFSelect_RetDone
from OCP.Interface import Interface_Static
from OCP.Quantity import Quantity_Color, Quantity_TOC_RGB
from OCP.ShapeFix import ShapeFix_ShapeTolerance
from OCP.STEPCAFControl import STEPCAFControl_Writer
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDF import TDF_Label, TDF_LabelSequence
from OCP.TDocStd import TDocStd_Document
from OCP.TopAbs import TopAbs_FACE
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Compound, TopoDS_Shell
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import XCAFDoc_ColorSurf, XCAFDoc_DocumentTool

from meshpipeline.cad.repair_v4.verdict.valid import check_valid
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool

CALL_TIMEOUT_S = 180


@pytest.fixture(scope="module")
def pool() -> Iterator[WorkerPool]:
    p = WorkerPool(1, start_timeout_s=120)
    try:
        yield p
    finally:
        p.close()


def _box(x: float = 0.0, y: float = 0.0, z: float = 0.0, size: float = 10.0):
    return BRepPrimAPI_MakeBox(gp_Pnt(x, y, z), size, size, size).Shape()


def _compound(*shapes):
    comp = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(comp)
    for shape in shapes:
        builder.Add(comp, shape)
    return comp


def _write_plain(shape, path: Path, unit: str = "MM") -> str:
    Interface_Static.SetCVal_s("write.step.unit", unit)
    try:
        writer = STEPControl_Writer()
        writer.Transfer(shape, STEPControl_AsIs)
        assert writer.Write(str(path)) == IFSelect_RetDone
    finally:
        Interface_Static.SetCVal_s("write.step.unit", "MM")
    return str(path)


def _set_name(label, text: str) -> None:
    TDataStd_Name.Set_s(label, TCollection_ExtendedString(text))


def _write_named_assembly(path: Path, shapes, names, color=None) -> str:
    """An XCAF assembly with one named component per shape, written through STEPCAFControl."""
    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    app.InitDocument(doc)
    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    color_tool = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
    top = shape_tool.AddShape(_compound(*shapes), True)
    _set_name(top, "assembly")
    components = TDF_LabelSequence()
    shape_tool.GetComponents_s(top, components)
    assert components.Length() == len(shapes)
    for index, name in enumerate(names, start=1):
        label = components.Value(index)
        ref = TDF_Label()
        shape_tool.GetReferredShape_s(label, ref)
        _set_name(label, f"{name}-instance")
        _set_name(ref, name)
        if color is not None:
            color_tool.SetColor(ref, Quantity_Color(*color, Quantity_TOC_RGB), XCAFDoc_ColorSurf)
    writer = STEPCAFControl_Writer()
    writer.SetNameMode(True)
    writer.SetColorMode(True)
    assert writer.Transfer(doc, STEPControl_AsIs)
    assert writer.Write(str(path)) == IFSelect_RetDone
    return str(path)


def _faces(shape) -> list:
    out = []
    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    while explorer.More():
        out.append(TopoDS.Face_s(explorer.Current()))
        explorer.Next()
    return out


def _kernel_check(pool: WorkerPool, shape, tmp_path: Path) -> dict:
    """Run the kernel `check_valid` on a BREP directly, for defects STEP cannot carry."""
    brep = tmp_path / "shape.brep"
    assert BRepTools.Write_s(shape, str(brep))
    result = pool.call("check_valid", {"brep": str(brep)}, timeout_s=CALL_TIMEOUT_S)
    assert result.ok, result.error
    assert result.value is not None
    return result.value


def _codes(findings) -> set[str]:
    return {f.code for f in findings}


def test_valid_box_is_ok(pool: WorkerPool, tmp_path: Path) -> None:
    step = _write_plain(_box(), tmp_path / "box.step")
    report = check_valid(pool, step, original_units=None)
    assert report.hard == []
    assert report.ok is True
    assert report.solids == 1
    assert report.units == "mm"
    assert report.round_trip_ok is True


def test_box_missing_a_face_is_open_shell(pool: WorkerPool, tmp_path: Path) -> None:
    faces = []
    explorer = TopExp_Explorer(_box(), TopAbs_FACE)
    while explorer.More():
        faces.append(TopoDS.Face_s(explorer.Current()))
        explorer.Next()
    shell = TopoDS_Shell()
    builder = BRep_Builder()
    builder.MakeShell(shell)
    for face in faces[:-1]:  # one face removed
        builder.Add(shell, face)
    solid = BRepBuilderAPI_MakeSolid(shell).Solid()
    report = check_valid(pool, _write_plain(solid, tmp_path / "open.step"), original_units=None)
    assert "open_shell" in _codes(report.hard)
    assert report.ok is False


def test_overlapping_boxes_are_body_overlap(pool: WorkerPool, tmp_path: Path) -> None:
    step = _write_plain(_compound(_box(0), _box(5)), tmp_path / "overlap.step")
    report = check_valid(pool, step, original_units=None)
    assert "body_overlap" in _codes(report.hard)
    assert report.ok is False
    assert report.solids == 2


def test_boxes_sharing_a_face_via_xcaf_are_ok(pool: WorkerPool, tmp_path: Path) -> None:
    """Review Focus 1: conformal contact is valid, including after the STEP round trip."""
    step = _write_named_assembly(tmp_path / "touch.step", [_box(0), _box(10)], ["left", "right"])
    report = check_valid(pool, step, original_units=None)
    assert report.hard == [], [(f.code, f.detail) for f in report.hard]
    assert report.ok is True
    assert report.solids == 2
    assert report.round_trip_ok is True


def test_inch_step_keeps_its_unit(pool: WorkerPool, tmp_path: Path) -> None:
    """Review Focus 2: the unit is preserved, never normalised to mm."""
    step = _write_plain(_box(), tmp_path / "inch.step", unit="INCH")
    report = check_valid(pool, step, original_units="inch")
    assert report.units == "inch"
    assert "units_changed" not in _codes(report.hard)
    assert report.ok is True
    assert report.round_trip_ok is True


def test_a_file_whose_unit_differs_from_the_original_is_units_changed(pool: WorkerPool, tmp_path: Path) -> None:
    step = _write_plain(_box(), tmp_path / "mm.step")
    report = check_valid(pool, step, original_units="inch")
    assert report.units == "mm"
    assert "units_changed" in _codes(report.hard)
    assert report.ok is False


def test_surface_only_step_has_no_solids(pool: WorkerPool, tmp_path: Path) -> None:
    """Review Focus 3: a file of faces and no solid is a hard fail, not a pass by absence."""
    face = BRepBuilderAPI_MakeFace(gp_Pln(), 0, 10, 0, 10).Face()
    report = check_valid(pool, _write_plain(face, tmp_path / "surface.step"), original_units=None)
    assert "no_solids" in _codes(report.hard)
    assert report.ok is False
    assert report.solids == 0


def test_names_and_colours_survive_read_write_read(pool: WorkerPool, tmp_path: Path) -> None:
    step = _write_named_assembly(tmp_path / "named.step", [_box(0), _box(10)], ["left", "right"], color=(1.0, 0.0, 0.0))
    first = pool.call("read_step_xcaf", {"path": step, "brep_out": str(tmp_path / "a.brep")}, timeout_s=CALL_TIMEOUT_S)
    assert first.ok, first.error
    assert first.value is not None
    names = {entry["name"] for entry in first.value["labels"].values()}
    assert {"left", "right"} <= names

    written = pool.call(
        "write_step_xcaf",
        {
            "brep": first.value["brep"],
            "labels": first.value["labels"],
            "assembly_ids": first.value["assembly_ids"],
            "units": first.value["units"],
            "path": str(tmp_path / "out.step"),
        },
        timeout_s=CALL_TIMEOUT_S,
    )
    assert written.ok, written.error

    second = pool.call(
        "read_step_xcaf",
        {"path": str(tmp_path / "out.step"), "brep_out": str(tmp_path / "b.brep")},
        timeout_s=CALL_TIMEOUT_S,
    )
    assert second.ok, second.error
    assert second.value is not None
    assert second.value["labels"].keys() == first.value["labels"].keys()
    for stable_id, before in first.value["labels"].items():
        after = second.value["labels"][stable_id]
        assert after["name"] == before["name"]
        assert after["parent"] == before["parent"]
        assert (after["color"] is None) == (before["color"] is None)
        if before["color"] is not None:
            assert after["color"] == pytest.approx(before["color"], abs=1e-3)


def test_reversed_solid_is_non_positive_volume(pool: WorkerPool, tmp_path: Path) -> None:
    # STEP normalises orientation, so this defect can only be authored as BREP.
    found = _kernel_check(pool, _box().Reversed(), tmp_path)
    assert "non_positive_volume" in {f["code"] for f in found["hard"]}


def test_crossing_faces_are_self_intersection(pool: WorkerPool, tmp_path: Path) -> None:
    shell = TopoDS_Shell()
    builder = BRep_Builder()
    builder.MakeShell(shell)
    for face in _faces(_box(0)) + _faces(_box(5)):
        builder.Add(shell, face)
    found = _kernel_check(pool, BRepBuilderAPI_MakeSolid(shell).Solid(), tmp_path)
    assert "self_intersection" in {f["code"] for f in found["hard"]}


def test_three_faces_on_an_edge_are_non_manifold(pool: WorkerPool, tmp_path: Path) -> None:
    poly = BRepBuilderAPI_MakePolygon()
    for corner in [(0, 0, 0), (0, 0, 10), (-5, -5, 10), (-5, -5, 0)]:
        poly.Add(gp_Pnt(*corner))
    poly.Close()
    sewing = BRepBuilderAPI_Sewing(1e-6)
    sewing.SetNonManifoldMode(True)
    for face in _faces(_box(0)):
        sewing.Add(face)
    sewing.Add(BRepBuilderAPI_MakeFace(poly.Wire()).Face())
    sewing.Perform()
    found = _kernel_check(pool, sewing.SewedShape(), tmp_path)
    assert "non_manifold" in {f["code"] for f in found["hard"]}


def test_soft_findings_are_reported_but_never_fail(pool: WorkerPool, tmp_path: Path) -> None:
    loose = _box()
    ShapeFix_ShapeTolerance().SetTolerance(loose, 2.0)
    tiny = _compound(_box(), _box(20, 0, 0, size=1e-4))
    for shape, expected in ((loose, "large_tolerance"), (tiny, "small_edge")):
        found = _kernel_check(pool, shape, tmp_path)
        assert expected in {f["code"] for f in found["soft"]}
        assert found["hard"] == []
    # And through the full check, a file with a tiny body still passes.
    report = check_valid(pool, _write_plain(tiny, tmp_path / "tiny.step"), original_units=None)
    assert report.ok is True
    assert {"small_edge", "small_face"} <= _codes(report.soft)
