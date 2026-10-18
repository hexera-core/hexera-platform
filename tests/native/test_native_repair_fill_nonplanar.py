# Responsibility: Verify on the real kernel that a small curved opening is closed by the non-planar fill, and only after the planar fill has declined it.
from __future__ import annotations

import pytest
from tests.cad_fixtures import write_cylinder_cap_hole_step

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair import loop
from meshpipeline.cad.repair.contracts import DefectCode

pytest.importorskip("OCP.BRepFill", reason="the CAD kernel is a native-tier dependency")


@pytest.fixture(autouse=True)
def _opted_in(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENABLED", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_DEVIATION_RATIO", 0.001)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_TOLERANCE_MM", 0.1)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ALLOW_FACE_REMOVAL", False)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_AREA_CHANGE_RATIO", 0.02)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO", 0.02)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_OPS", 6)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_STALL_LIMIT", 2)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_TIMEOUT_S", 120.0)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_HOLE_SPAN_RATIO", 0.75)


def _read(path):
    from meshpipeline.cad.repair.brep import _read_shape

    return _read_shape(path)


def _closed_free_loops(shape):
    from OCP.ShapeAnalysis import ShapeAnalysis_FreeBounds
    from OCP.TopAbs import TopAbs_WIRE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    wires = []
    explorer = TopExp_Explorer(ShapeAnalysis_FreeBounds(shape).GetClosedWires(), TopAbs_WIRE)
    while explorer.More():
        wires.append(TopoDS.Wire_s(explorer.Current()))
        explorer.Next()
    return wires


def test_the_fixture_has_one_small_curved_opening_no_flat_lid_fits(tmp_path):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace

    from meshpipeline.cad.repair.conservative import _span
    from meshpipeline.cad.repair.localize import _diagonal

    shape = _read(write_cylinder_cap_hole_step(tmp_path / "cap.step"))
    loops = _closed_free_loops(shape)
    assert len(loops) == 1
    assert not BRepBuilderAPI_MakeFace(loops[0], True).IsDone()
    assert _span(loops[0]) / _diagonal(shape) < 0.75


def test_a_curved_opening_is_closed_by_the_nonplanar_fill(tmp_path, monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FILL_PLANAR_HOLES", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FILL_NONPLANAR_HOLES", True)
    # THREE MISSES, NOT THE SHIPPED TWO. On this bare shell the rules planner tries sew_faces
    # (rolled back: target_remains) and fill_planar_hole (declined: not_planar) first, and at
    # CAD_REPAIR_STALL_LIMIT=2 the run stalls before the curved fill is ever proposed. The plan
    # keeps the default at 2, so this test opts in to reach the operation it is about.
    monkeypatch.setattr(rcfg, "CAD_REPAIR_STALL_LIMIT", 3)
    shape = _read(write_cylinder_cap_hole_step(tmp_path / "cap.step"))
    run = loop.run(shape, tolerance_mm=0.01)
    ops = [(v.op, v.status, v.reason) for v in run.verdicts]
    assert ("fill_planar_hole", "rolled_back", "declined:not_planar") in ops
    kept = [v for v in run.verdicts if v.kept]
    assert [v.op for v in kept] == ["fill_nonplanar_hole"]
    assert kept[0].basis in ("geometric", "kernel")
    assert not any(d.code is DefectCode.open_shell for d in run.defects)
    # 20 mm x the 60deg arc of r=20 is 418.9 mm2; a constrained patch over it is within 2%.
    assert kept[0].evidence["area_delta_mm2"] == pytest.approx(418.9, rel=0.02)
    assert kept[0].evidence["builder"] == "BRepFill_Filling"
    assert kept[0].evidence["g1_achieved"] is True


@pytest.mark.parametrize("builder", ["g1", "g0", "plate"])
def test_every_builder_closes_the_curved_opening_within_tolerance(tmp_path, builder):
    # The fallbacks are reached only when the builder before them fails, which the fixture never
    # makes happen - so each is driven directly here, or it would ship untested.
    import meshpipeline.cad.repair.operations as operations
    from meshpipeline.cad.repair.localize import _face_area

    shape = _read(write_cylinder_cap_hole_step(tmp_path / "cap.step"))
    wire = _closed_free_loops(shape)[0]
    edges = operations._wire_edges(wire)
    if builder == "plate":
        face, g0, g1 = operations._plate(wire)
    else:
        supports = operations._support_faces(shape, edges) if builder == "g1" else None
        face, g0, g1 = operations._filling(edges, supports)
    assert g0 <= 0.01 and g1 is (builder == "g1")
    assert not operations._bulges(face, wire)
    assert 0.9 * 418.9 < _face_area(face) < 1.1 * 418.9
    sewn = operations._sew_patch(shape, face, tolerance_mm=0.01)
    assert sewn is not None and _closed_free_loops(sewn[0]) == []


def _max_tolerance(shape) -> float:
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_VERTEX
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    worst = 0.0
    for kind, cast in ((TopAbs_EDGE, TopoDS.Edge_s), (TopAbs_VERTEX, TopoDS.Vertex_s)):
        explorer = TopExp_Explorer(shape, kind)
        while explorer.More():
            worst = max(worst, BRep_Tool.Tolerance_s(cast(explorer.Current())))
            explorer.Next()
    return worst


def _brep_dump(shape, path) -> bytes:
    from OCP.BRepTools import BRepTools

    BRepTools.Write_s(shape, str(path))
    return path.read_bytes()


def test_a_declined_plate_patch_leaves_the_input_shape_byte_identical(tmp_path, monkeypatch):
    # THE PLATE PATH ONLY: MakeFace(surface, wire) + ShapeFix_Face write pcurves and raised
    # tolerances into the edges they are given. On the caller's own free-loop edges that damaged
    # the input (1e-7 -> 7.8e-4 mm) even on a decline, and the session's rollback snapshot is the
    # same object. Both filling builds are switched off so GeomPlate is the one that runs, and a
    # 1e-9 mm tolerance makes it decline boundary_error.
    import meshpipeline.cad.repair.operations as operations
    from meshpipeline.cad.repair.identity import EntityRef

    monkeypatch.setattr(rcfg, "CAD_REPAIR_FILL_NONPLANAR_HOLES", True)
    monkeypatch.setattr(operations, "_filling", lambda edges, supports: None)
    shape = _read(write_cylinder_cap_hole_step(tmp_path / "cap.step"))
    index = next(i for i in range(1, 64) if operations.free_wire_containing(shape, i) is not None)
    tolerance_before = _max_tolerance(shape)
    dump_before = _brep_dump(shape, tmp_path / "before.brep")

    out = operations.FillNonplanarHole().apply(
        shape, (EntityRef(entity_type="edge", fingerprint="edge:rim", entity_index=index),),
        tolerance_mm=1e-9)

    assert out.shape is shape and "boundary_error" in out.evidence["declined"]
    assert out.evidence["builder"] == "GeomPlate"
    assert _max_tolerance(shape) == tolerance_before
    assert _brep_dump(shape, tmp_path / "after.brep") == dump_before
