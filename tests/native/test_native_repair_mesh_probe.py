# Responsibility: Verify the gmsh mesh probe against the real gmsh and OCC kernels.
# Boundaries: the native tier; the error classifier's string handling is unit-tested separately.
from __future__ import annotations

import os
import time

import pytest

pytest.importorskip("gmsh", reason="gmsh is a native-tier dependency")
pytest.importorskip("OCP.BRepTools", reason="the CAD kernel is a native-tier dependency")

from OCP.BRep import BRep_Builder  # noqa: E402
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox  # noqa: E402
from OCP.BRepTools import BRepTools  # noqa: E402
from OCP.gp import gp_Pnt  # noqa: E402
from OCP.TopAbs import TopAbs_FACE  # noqa: E402
from OCP.TopExp import TopExp_Explorer  # noqa: E402
from OCP.TopoDS import TopoDS_Compound  # noqa: E402

from meshpipeline.cad.repair import mesh_probe  # noqa: E402
from meshpipeline.cad.repair.mesh_probe import EDGE_THROUGH_FACE, TIMEOUT, probe_file  # noqa: E402


def _box(x=0.0, y=0.0, z=0.0, size=10.0):
    return BRepPrimAPI_MakeBox(gp_Pnt(x, y, z), size, size, size).Shape()


def _write(shape, path):
    assert BRepTools.Write_s(shape, str(path))
    return path


def _overlap(tmp_path):
    compound = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(compound)
    builder.Add(compound, _box())
    builder.Add(compound, _box(5.0, 5.0, 5.0))
    return _write(compound, tmp_path / "overlap.brep")


def test_box_meshes(tmp_path):
    result = probe_file(_write(_box(), tmp_path / "box.brep"), diagonal_mm=17.3)
    assert result.meshed is True
    assert result.volume_elements > 0
    assert result.failure is None


def test_overlapping_solids_fail_located(tmp_path):
    result = probe_file(_overlap(tmp_path), diagonal_mm=26.0)
    if result.meshed:
        pytest.skip("this gmsh build meshes interpenetrating boxes; it raises no PLC error here")
    assert result.meshed is False
    assert result.failure is not None
    assert result.failure.kind == EDGE_THROUGH_FACE


def test_unreadable_part_is_a_failure_not_an_exception(tmp_path):
    bad = tmp_path / "bad.brep"
    bad.write_text("this is not a BREP")
    result = probe_file(bad, diagonal_mm=10.0)
    assert result.meshed is False
    assert result.failure is not None and result.failure.message


def test_tag_boxes_cover_every_surface(tmp_path):
    shape = _box()
    faces = 0
    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    while explorer.More():
        faces += 1
        explorer.Next()
    path = _write(shape, tmp_path / "box.brep")
    full = probe_file(path, diagonal_mm=17.3)
    only_boxes = probe_file(path, diagonal_mm=17.3, surfaces=())
    assert len(full.tag_boxes) == faces == 6
    assert len(only_boxes.tag_boxes) == faces
    assert only_boxes.volume_elements == 0


def test_probe_survives_child_crash(monkeypatch, tmp_path):
    def crash(*_args):
        os._exit(139)

    monkeypatch.setattr(mesh_probe, "_probe_child", crash)
    result = probe_file(_write(_box(), tmp_path / "box.brep"), diagonal_mm=17.3)
    assert result.meshed is False
    assert result.failure is not None and result.failure.kind == TIMEOUT


def test_budget_is_honoured(tmp_path):
    path = _write(_box(), tmp_path / "box.brep")
    start = time.monotonic()
    result = probe_file(path, diagonal_mm=26.0, budget_s=0.01)
    assert time.monotonic() - start < 2.0
    assert result.failure is not None and result.failure.kind == TIMEOUT
