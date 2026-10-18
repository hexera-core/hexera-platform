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


@pytest.mark.xfail(strict=True, reason="fill_nonplanar_hole not yet implemented")
def test_a_curved_opening_is_closed_by_the_nonplanar_fill(tmp_path, monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FILL_PLANAR_HOLES", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FILL_NONPLANAR_HOLES", True)
    shape = _read(write_cylinder_cap_hole_step(tmp_path / "cap.step"))
    run = loop.run(shape, tolerance_mm=0.01)
    ops = [(v.op, v.status, v.reason) for v in run.verdicts]
    assert ("fill_planar_hole", "rolled_back", "declined:not_planar") in ops
    kept = [v for v in run.verdicts if v.kept]
    assert [v.op for v in kept] == ["fill_nonplanar_hole"]
    assert kept[0].basis in ("geometric", "kernel")
    assert not any(d.code is DefectCode.open_shell for d in run.defects)
    assert 0 < kept[0].evidence["area_delta_mm2"] < 2 * 418.9   # 20 * 60deg arc of r=20
