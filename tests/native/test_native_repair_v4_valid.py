# Responsibility: Verify layer 1 of the v4 checker (valid) and the XCAF round trip against the real
#                 kernel, on fixtures built here: conformal contact is not overlap, inch STEP keeps
#                 its unit, a surface-only file has no solids, names survive read -> write -> read.
# Boundaries: the native tier - the only place OpenCASCADE exists. The kernel under test runs in a
#             WorkerPool process; this module touches OCP only to author fixtures.
from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("OCP.XCAFDoc", reason="the CAD kernel is a native-tier dependency")

from OCP.BRep import BRep_Builder
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakePolygon,
    BRepBuilderAPI_MakeSolid,
    BRepBuilderAPI_Sewing,
)
from OCP.BRepGProp import BRepGProp
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakeSphere
from OCP.BRepTools import BRepTools
from OCP.gp import gp_Ax2, gp_Dir, gp_Pln, gp_Pnt
from OCP.GProp import GProp_GProps
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
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Compound, TopoDS_Iterator, TopoDS_Shape, TopoDS_Shell
from OCP.TopTools import TopTools_IndexedMapOfShape
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
    writer = STEPControl_Writer()  # first: the unit parameter only exists once a writer does
    assert Interface_Static.SetCVal_s("write.step.unit", unit)
    try:
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
    codes = {f["code"] for f in found["hard"]}
    assert "self_intersection" in codes
    assert "brep_invalid" in codes  # BRepCheck_Analyzer rejects the crossing shell too


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


def _write_one_product(
    path: Path, shape, *, faces: dict[str, tuple[tuple[float, float, float], tuple]] | None = None
) -> str:
    """One XCAF product (not an assembly), optionally with named, coloured faces.

    `faces` maps a face name to (centroid, rgb); the face is found by its centroid.
    """
    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    app.InitDocument(doc)
    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    color_tool = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
    top = shape_tool.AddShape(shape, False)
    _set_name(top, "part")
    for name, (centroid, rgb) in (faces or {}).items():
        match = [f for f in _faces(shape) if _near(_centroid(f), centroid)]
        assert len(match) == 1, (name, centroid)
        sub = shape_tool.AddSubShape(top, match[0])
        _set_name(sub, name)
        color_tool.SetColor(sub, Quantity_Color(*rgb, Quantity_TOC_RGB), XCAFDoc_ColorSurf)
    writer = STEPCAFControl_Writer()
    writer.SetNameMode(True)
    writer.SetColorMode(True)
    # After the writer exists (constructing it resets the parameters); the default 0 drops face names.
    assert Interface_Static.SetIVal_s("write.stepcaf.subshapes.name", 1)
    assert writer.Transfer(doc, STEPControl_AsIs)
    assert writer.Write(str(path)) == IFSelect_RetDone
    return str(path)


def _centroid(face) -> tuple[float, float, float]:
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, props)
    c = props.CentreOfMass()
    return (c.X(), c.Y(), c.Z())


def _near(a, b, tol: float = 1e-6) -> bool:
    return all(abs(x - y) < tol for x, y in zip(a, b, strict=True))


def _first_child_faces(brep: str) -> TopTools_IndexedMapOfShape:
    """The face map of the first free shape in a BREP the kernel wrote: what the ids' F<k> index into."""
    root = TopoDS_Shape()
    assert BRepTools.Read_s(root, brep, BRep_Builder())
    child = TopoDS_Iterator(root).Value()
    faces = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(child, TopAbs_FACE, faces)
    return faces


def _read(pool: WorkerPool, path: str, brep: Path) -> dict:
    result = pool.call("read_step_xcaf", {"path": path, "brep_out": str(brep)}, timeout_s=CALL_TIMEOUT_S)
    assert result.ok, result.error
    assert result.value is not None
    return result.value


def _round_trip_faces(pool: WorkerPool, tmp_path: Path, wanted: dict) -> list[tuple[dict, str]]:
    """(label map, brep) of the original read and of the write -> STEP -> read."""
    step = _write_one_product(tmp_path / "faces.step", _box(), faces=wanted)
    first = _read(pool, step, tmp_path / "a.brep")
    written = pool.call(
        "write_step_xcaf",
        {
            "brep": first["brep"],
            "labels": first["labels"],
            "assembly_ids": first["assembly_ids"],
            "units": first["units"],
            "path": str(tmp_path / "out.step"),
        },
        timeout_s=CALL_TIMEOUT_S,
    )
    assert written.ok, written.error
    second = _read(pool, str(tmp_path / "out.step"), tmp_path / "b.brep")
    return [(first["labels"], first["brep"]), (second["labels"], second["brep"])]


WANTED_FACES = {"top": ((5.0, 5.0, 10.0), (1.0, 0.0, 0.0)), "inlet": ((0.0, 5.0, 5.0), (0.0, 0.0, 1.0))}


def test_face_colours_survive_by_id_and_by_geometry(pool: WorkerPool, tmp_path: Path) -> None:
    """A face colour must stay on the same GEOMETRIC face across write -> STEP -> read, not just on an id."""
    for label_map, brep in _round_trip_faces(pool, tmp_path, WANTED_FACES):
        faces = _first_child_faces(brep)
        coloured = {tuple(entry["color"]): key for key, entry in label_map.items() if "/F" in key and entry["color"]}
        assert len(coloured) == 2, label_map
        for centroid, rgb in WANTED_FACES.values():
            index = int(coloured[rgb].rsplit("/F", 1)[1])
            assert _near(_centroid(TopoDS.Face_s(faces.FindKey(index))), centroid, 1e-4), (rgb, coloured)


def test_face_names_survive_by_id_and_by_geometry(pool: WorkerPool, tmp_path: Path) -> None:
    for label_map, brep in _round_trip_faces(pool, tmp_path, WANTED_FACES):
        faces = _first_child_faces(brep)
        named = {entry["name"]: key for key, entry in label_map.items() if "/F" in key and entry["name"]}
        assert set(named) == {"top", "inlet"}, label_map
        for name, (centroid, _rgb) in WANTED_FACES.items():
            index = int(named[name].rsplit("/F", 1)[1])
            assert _near(_centroid(TopoDS.Face_s(faces.FindKey(index))), centroid, 1e-4), name


def test_a_face_without_a_name_does_not_crash_the_reader(pool: WorkerPool, tmp_path: Path) -> None:
    """Regression: FindAttribute on a name-less sub-shape label segfaulted the worker."""
    first = _read(pool, _write_one_product(tmp_path / "c.step", _box(), faces=WANTED_FACES), tmp_path / "c.brep")
    assert any("/F" in key for key in first["labels"])


def test_touching_compound_in_one_product_is_ok(pool: WorkerPool, tmp_path: Path) -> None:
    """The common multi-body shape: ONE product holding two touching solids, which STEP may duplicate faces for."""
    step = _write_one_product(tmp_path / "one.step", _compound(_box(0), _box(10)))
    report = check_valid(pool, step, original_units=None)
    assert report.hard == [], [(f.code, f.detail) for f in report.hard]
    assert report.solids == 2
    assert report.round_trip_ok is True


def test_curved_contact_is_ok(pool: WorkerPool, tmp_path: Path) -> None:
    """A plug in a bored block shares a cylindrical face: conformal curved contact is valid."""
    axis = gp_Ax2(gp_Pnt(5, 5, -1), gp_Dir(0, 0, 1))
    block = BRepAlgoAPI_Cut(_box(0), BRepPrimAPI_MakeCylinder(axis, 3.3, 12).Shape()).Shape()
    plug = BRepAlgoAPI_Common(BRepPrimAPI_MakeCylinder(axis, 3.3, 12).Shape(), _box(0)).Shape()
    step = _write_named_assembly(tmp_path / "curved.step", [block, plug], ["block", "plug"])
    report = check_valid(pool, step, original_units=None)
    assert report.hard == [], [(f.code, f.detail) for f in report.hard]
    assert report.solids == 2
    assert report.round_trip_ok is True


def test_tangent_curved_contact_is_ok(pool: WorkerPool, tmp_path: Path) -> None:
    """Sphere on a plane and cylinder on a plane touch at a point and a line: where Common is least robust."""
    sphere = BRepPrimAPI_MakeSphere(gp_Pnt(5, 5, 15), 5.0).Shape()
    lying = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 5, 15), gp_Dir(1, 0, 0)), 5.0, 10.0).Shape()
    for name, curved in (("sphere", sphere), ("cylinder", lying)):
        step = _write_named_assembly(tmp_path / f"{name}.step", [_box(0), curved], ["base", name])
        report = check_valid(pool, step, original_units=None)
        assert report.hard == [], (name, [(f.code, f.detail) for f in report.hard])
        assert report.solids == 2


def test_interpenetration_below_the_tolerance_is_contact(pool: WorkerPool, tmp_path: Path) -> None:
    """1e-8 to 1e-7 of interpenetration survives a STEP round trip as noise; 1e-3 is a real overlap."""
    for depth in (1e-8, 1e-7):
        step = _write_named_assembly(tmp_path / f"d{depth}.step", [_box(0), _box(10 - depth)], ["a", "b"])
        report = check_valid(pool, step, original_units=None)
        assert report.hard == [], (depth, [(f.code, f.detail) for f in report.hard])
    step = _write_named_assembly(tmp_path / "deep.step", [_box(0), _box(10 - 1e-3)], ["a", "b"])
    assert "body_overlap" in _codes(check_valid(pool, step, original_units=None).hard)


def test_a_boolean_that_does_not_complete_is_reported_with_boolean_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fail-closed branch, driven in-process: the Boolean is the one thing replaced."""
    from meshpipeline.cad.repair_v4.verdict import valid_kernel

    monkeypatch.setattr(valid_kernel, "_common", lambda a, b, fuzzy: None)
    found = valid_kernel._overlapping_pairs([_box(0), _box(5)], [1000.0, 1000.0])
    assert [f["code"] for f in found] == ["body_overlap"]
    assert found[0]["detail"] == {"boolean_failed": True}


def test_the_fuzzy_retry_completes_a_boolean(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the plain Common does not complete, the fuzzy one decides instead of failing the file."""
    from OCP import BRepAlgoAPI

    from meshpipeline.cad.repair_v4.verdict import valid_kernel

    real = BRepAlgoAPI.BRepAlgoAPI_Common

    class _NotDone:
        def IsDone(self) -> bool:
            return False

    calls = []

    class Wrapper:
        def __new__(cls, *args):
            if args:
                calls.append("plain")
                return _NotDone()
            calls.append("fuzzy")
            return real()

    monkeypatch.setattr(BRepAlgoAPI, "BRepAlgoAPI_Common", Wrapper)
    result = valid_kernel._common(_box(0), _box(5), 1e-5)
    assert calls == ["plain", "fuzzy"]
    assert result is not None
    assert valid_kernel._volume(result.Shape()) == pytest.approx(500.0)


def test_unreadable_step_is_brep_invalid(pool: WorkerPool, tmp_path: Path) -> None:
    junk = tmp_path / "junk.step"
    junk.write_text("this is not a STEP file")
    report = check_valid(pool, str(junk), original_units=None)
    assert _codes(report.hard) == {"brep_invalid"}
    assert report.ok is False


def test_unit_spellings_are_normalised(pool: WorkerPool, tmp_path: Path) -> None:
    step = _write_plain(_box(), tmp_path / "in.step", unit="INCH")
    for spelling in ("in", "inches", "inch"):
        assert check_valid(pool, step, original_units=spelling).ok is True


def test_micrometre_file_round_trips(pool: WorkerPool, tmp_path: Path) -> None:
    step = _write_plain(_box(), tmp_path / "um.step", unit="UM")
    report = check_valid(pool, step, original_units=None)
    assert report.units == "um"
    assert report.ok is True


def test_face_names_are_written_to_step_when_the_switch_is_set(tmp_path: Path) -> None:
    """The writer puts the XCAF face name into ADVANCED_FACE once write.stepcaf.subshapes.name is set after construction."""
    text = Path(_write_one_product(tmp_path / "named.step", _box(), faces=WANTED_FACES)).read_text()
    assert "ADVANCED_FACE('top'," in text and "ADVANCED_FACE('inlet'," in text


def test_the_subshape_name_switch_only_takes_after_the_writer_exists(tmp_path: Path) -> None:
    """Why xcaf_io sets parameters after constructing the reader/writer: set earlier, in a fresh process, they are ignored."""
    code = (
        "from OCP.Interface import Interface_Static\n"
        "from OCP.STEPCAFControl import STEPCAFControl_Writer\n"
        "before = Interface_Static.SetIVal_s('write.stepcaf.subshapes.name', 1)\n"
        "STEPCAFControl_Writer()\n"
        "after = Interface_Static.SetIVal_s('write.stepcaf.subshapes.name', 1)\n"
        "print(before, after)\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.split()
    assert out[-2:] == ["False", "True"]


def test_a_non_mm_write_as_the_first_call_in_a_fresh_worker_keeps_its_unit(tmp_path: Path) -> None:
    """Regression: write.step.unit was ignored until a writer existed, so a first-call inch write came out in mm."""
    brep = tmp_path / "box.brep"
    assert BRepTools.Write_s(_compound(_box()), str(brep))
    fresh = WorkerPool(1, start_timeout_s=120)
    try:
        written = fresh.call(
            "write_step_xcaf",
            {
                "brep": str(brep),
                "labels": {"0": {"name": "p", "color": None, "parent": None}},
                "assembly_ids": [],
                "units": "inch",
                "path": str(tmp_path / "first.step"),
            },
            timeout_s=CALL_TIMEOUT_S,
        )
    finally:
        fresh.close()
    assert written.ok, written.error
    assert "CONVERSION_BASED_UNIT('INCH'" in (tmp_path / "first.step").read_text()


def test_yard_file_is_not_a_round_trip_failure(pool: WorkerPool, tmp_path: Path) -> None:
    """A unit OCCT cannot write is a legitimate file: it is reported in its own unit and passes."""
    text = Path(_write_plain(_box(), tmp_path / "inch.step", unit="INCH")).read_text()
    assert "CONVERSION_BASED_UNIT('INCH'" in text and "LENGTH_MEASURE_WITH_UNIT(25.4," in text
    yard = tmp_path / "yard.step"
    yard.write_text(
        text.replace("'INCH'", "'YARD'").replace("LENGTH_MEASURE_WITH_UNIT(25.4,", "LENGTH_MEASURE_WITH_UNIT(914.4,")
    )
    report = check_valid(pool, str(yard), original_units="yard")
    assert report.units == "yard"
    assert report.hard == [], [(f.code, f.detail) for f in report.hard]
    assert report.round_trip_ok is True


# --- unbounded input: layer 1 says "outside what the definition can judge" before anything meshes it ---------


def _with_infinite_line(solid):
    """`solid` plus an edge on an unbounded Geom_Line: it survives STEP and gives the +-1e100 box the corpus showed."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge
    from OCP.gp import gp_Lin

    return _compound(solid, BRepBuilderAPI_MakeEdge(gp_Lin(gp_Pnt(0, 0, 0), gp_Dir(1, 1, 1))).Edge())


def test_a_solid_carrying_an_infinite_curve_is_unbounded_not_valid(pool: WorkerPool, tmp_path: Path) -> None:
    step = _write_plain(_with_infinite_line(_box()), tmp_path / "unbounded.step")
    report = check_valid(pool, step, original_units=None)
    assert report.unbounded and report.ok is False
    assert _codes(report.hard) == {"unbounded_geometry"}  # and nothing else: the rest was not run
    (finding,) = report.hard
    assert finding.detail["diagonal_mm"] > 1e99 and finding.detail["limit_mm"] == 1e9
    assert report.solids == 1 and report.round_trip_ok is False


def test_an_infinite_half_space_is_unbounded(pool: WorkerPool, tmp_path: Path) -> None:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeHalfSpace

    face = BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1))).Face()
    half = BRepPrimAPI_MakeHalfSpace(face, gp_Pnt(0, 0, -1)).Solid()
    found = _kernel_check(pool, half, tmp_path)
    assert [f["code"] for f in found["hard"]] == ["unbounded_geometry"]
    assert found["hard"][0]["detail"]["open_sides"]  # OCCT marks the box open on the infinite sides


@pytest.mark.parametrize(
    "dims", [(200_000.0, 200_000.0, 200_000.0), (100_000_000.0, 3_000.0, 3_000.0), (4.4, 1.0, 1.0)],
    ids=["200m-cube", "100km-beam", "corpus-minimum-4mm"],
)
def test_a_legitimate_large_or_small_part_is_not_unbounded(pool: WorkerPool, tmp_path: Path, dims) -> None:
    step = _write_plain(BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), *dims).Shape(), tmp_path / "part.step")
    report = check_valid(pool, step, original_units=None)
    assert not report.unbounded
    assert "unbounded_geometry" not in _codes(report.hard)


def test_the_verdict_on_an_unbounded_file_is_out_of_scope_and_nothing_meshes_it(pool: WorkerPool, tmp_path: Path) -> None:
    from meshpipeline.cad.repair_v4.verdict import verdict

    def panel_must_not_run(*args, **kwargs):
        raise AssertionError("an unbounded input was handed to the panel")

    step = _write_plain(_with_infinite_line(_box()), tmp_path / "unbounded.step")
    v = verdict(pool, step, step, members=[object()], run_panel=panel_must_not_run)
    assert v.out_of_scope and not v.clean and not v.inconclusive and v.counts_for_scoreboard
    assert v.panel == [] and v.faithful is None


def test_check_shape_without_booleans_skips_exactly_the_two_boolean_rules(tmp_path: Path) -> None:
    """C16 lever b': the round-trip re-check of an already-invalid file runs without its Booleans."""
    from meshpipeline.cad.repair_v4.verdict.valid_kernel import check_shape

    brep = str(tmp_path / "two.brep")
    BRepTools.Write_s(_compound(_box(), _box(x=5.0)), brep)  # two boxes overlapping by half
    assert "body_overlap" in {f["code"] for f in check_shape(brep)["hard"]}
    skipped = check_shape(brep, booleans=False)
    assert skipped["solids"] == 2 and not {f["code"] for f in skipped["hard"]} & {"body_overlap", "self_intersection"}
