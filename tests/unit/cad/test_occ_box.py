# Responsibility: Verify a part's size is the box of its own surface, never OpenCascade's loose
# envelope of B-spline control points - in the box helper, the tessellation and the scout's form.
# Boundaries: real OpenCASCADE shapes; no model, no rendering.
from __future__ import annotations

import pytest

try:
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.Geom import Geom_BSplineSurface
    from OCP.gp import gp_Pnt
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    from OCP.TColgp import TColgp_Array2OfPnt
    from OCP.TColStd import TColStd_Array1OfInteger, TColStd_Array1OfReal, TColStd_Array2OfReal
except Exception:  # noqa: BLE001
    pytest.skip("OCP not available", allow_module_level=True)

from pathlib import Path

import numpy as np

from meshpipeline.cad import occ_box


def _sheet_with_a_far_pole(far: float = 100.0, weight: float = 1e-4):
    """A 1 x 1 patch (mm) whose middle control point sits `far` above it with a tiny weight: the
    surface barely lifts, but every pole is inside OpenCascade's loose envelope."""
    poles = TColgp_Array2OfPnt(1, 3, 1, 3)
    weights = TColStd_Array2OfReal(1, 3, 1, 3)
    for i in range(3):
        for j in range(3):
            z = far if (i, j) == (1, 1) else 0.0
            poles.SetValue(i + 1, j + 1, gp_Pnt(0.5 * i, 0.5 * j, z))
            weights.SetValue(i + 1, j + 1, weight if (i, j) == (1, 1) else 1.0)
    knots = TColStd_Array1OfReal(1, 2)
    knots.SetValue(1, 0.0)
    knots.SetValue(2, 1.0)
    mults = TColStd_Array1OfInteger(1, 2)
    mults.SetValue(1, 3)
    mults.SetValue(2, 3)
    surf = Geom_BSplineSurface(poles, weights, knots, knots, mults, mults, 2, 2)
    return BRepBuilderAPI_MakeFace(surf, 1e-7).Face()


def _ext(box):
    return [box[3] - box[0], box[4] - box[1], box[5] - box[2]]


def test_the_loose_envelope_reaches_the_far_pole_the_surface_box_does_not():
    face = _sheet_with_a_far_pole()
    assert _ext(occ_box.loose_box(face))[2] > 50.0, "the envelope encloses the far control point"
    box, _lin = occ_box.mesh_to_size(face, 1.0 / 2500.0, 0.3)
    ext = _ext(box)
    assert ext[0] == pytest.approx(1.0, abs=1e-6) and ext[1] == pytest.approx(1.0, abs=1e-6)
    assert ext[2] < 0.1, "the surface itself lifts a few hundredths, nowhere near 100"


def test_an_honest_envelope_meshes_exactly_as_before():
    # a plain box: envelope and surface agree, so the deflection stays the envelope's
    shape = BRepPrimAPI_MakeBox(10.0, 20.0, 30.0).Shape()
    env = occ_box.diagonal(occ_box.loose_box(shape))
    box, lin = occ_box.mesh_to_size(shape, 1.0 / 2500.0, 0.3)
    assert lin == pytest.approx(env / 2500.0)
    assert _ext(box) == pytest.approx([10.0, 20.0, 30.0], abs=1e-6)


def test_a_loose_envelope_is_meshed_again_at_the_real_size():
    face = _sheet_with_a_far_pole()
    env = occ_box.diagonal(occ_box.loose_box(face))
    box, lin = occ_box.mesh_to_size(face, 1.0 / 2500.0, 0.3)
    assert lin == pytest.approx(occ_box.diagonal(box) / 2500.0, rel=0.05)
    assert lin < env / 2500.0 / occ_box.REMESH_LOOSENESS


def test_an_explicit_deflection_is_kept():
    face = _sheet_with_a_far_pole()
    _box, lin = occ_box.mesh_to_size(face, 1.0 / 2500.0, 0.3, linear_deflection=0.01)
    assert lin == 0.01


def test_a_face_the_mesher_left_bare_still_counts():
    # two 1 mm cubes 50 mm apart; the far one loses its triangles, as a face the mesher fails on
    # does. The box must still reach it, not shrink to the cube that meshed.
    from OCP.BRep import BRep_Builder
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Compound

    near = BRepPrimAPI_MakeBox(1.0, 1.0, 1.0).Shape()
    far = BRepPrimAPI_MakeBox(gp_Pnt(50.0, 0.0, 0.0), 1.0, 1.0, 1.0).Shape()
    comp = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(comp)
    builder.Add(comp, near)
    builder.Add(comp, far)
    BRepMesh_IncrementalMesh(comp, 0.01, False, 0.3, True)
    BRepTools.Clean_s(far)
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS
    bare = TopExp_Explorer(far, TopAbs_FACE)
    assert BRep_Tool.Triangulation_s(TopoDS.Face_s(bare.Current()), TopLoc_Location()) is None
    box = occ_box.surface_box(comp)
    assert box is not None
    assert _ext(box) == pytest.approx([51.0, 1.0, 1.0], abs=1e-6)


def test_a_shape_with_no_face_meshed_reads_its_faces_not_the_envelope():
    # nothing meshed yet: every face is bare, so every face adds its exact box - the patch's own
    # few hundredths of lift, never the 100 mm control point the envelope holds
    face = _sheet_with_a_far_pole()
    box = occ_box.surface_box(face)
    assert box is not None
    assert _ext(box)[2] < 0.1


def test_nothing_meshable_has_no_surface_box():
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound
    comp = TopoDS_Compound()
    BRep_Builder().MakeCompound(comp)
    assert occ_box.surface_box(comp) is None


def _step(shape, path: Path) -> Path:
    w = STEPControl_Writer()
    assert Interface_Static.SetCVal_s("write.step.unit", "MM")
    w.Transfer(shape, STEPControl_AsIs)
    w.Write(str(path))
    return path


def _prepared(path: Path):
    from meshpipeline.cad.unit_evidence import parser_applied_unit
    from meshpipeline.contracts.coordinate_state import from_occ_transfer
    from meshpipeline.contracts.geometry_units import (
        GeometryInterpretation,
        LengthUnit,
        ResolutionBasis,
    )
    interp = GeometryInterpretation(
        interpretation_id="t", owner_id="t", geometry_source_id="t",
        unit=LengthUnit.millimetre, scale_to_metres=1e-3,
        basis=ResolutionBasis.file_declared, evidence="test")
    return from_occ_transfer(interp, parser_applied_unit(path))


def test_the_form_and_the_staged_surface_report_the_surface_size(tmp_path):
    from meshpipeline.cad.cad_tessellate import tessellate_to_stl
    from meshpipeline.cad.scout import scout_cad
    from meshpipeline.cad.stl_io import read_stl_triangles

    src = _step(_sheet_with_a_far_pole(), tmp_path / "sheet.step")
    facts = scout_cad(src, prepared=_prepared(src)).as_dict()
    assert facts["size_mm"][0] == pytest.approx(1.0, abs=0.01)
    assert facts["size_mm"][2] < 0.1, f"the form must not show the 100 mm control point: {facts}"
    stl = tessellate_to_stl(src, tmp_path / "sheet.stl", prepared=_prepared(src))
    pts = np.asarray(read_stl_triangles(stl), float).reshape(-1, 3)
    assert (pts.max(axis=0) - pts.min(axis=0))[2] < 1e-4


def test_the_gmsh_report_gives_the_builder_the_surface_size(tmp_path):
    pytest.importorskip("gmsh")
    from meshpipeline.engines.gmsh.gmsh_runner import inspect_stl

    _step(_sheet_with_a_far_pole(), tmp_path / "geometry.step")
    report = inspect_stl(tmp_path)
    x0, y0, z0, x1, y1, z1 = report["bbox"]
    assert x1 - x0 == pytest.approx(1.0, abs=0.01)
    assert z1 - z0 < 0.1, f"the builder must not be told about the 100 mm control point: {report['bbox']}"
