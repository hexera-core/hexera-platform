# Responsibility: Prove natively that the per-worker kernel memos of layers 1 and 3 are exact: they hit only for
#                 bit-identical shapes, and a hit returns what a fresh computation returns.
# Boundaries: calls the kernel functions in this process (as a worker would), so the memos can be inspected.
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("OCP.BinTools", reason="the CAD kernel is a native-tier dependency")

from OCP.BRep import BRep_Builder
from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.BRepTools import BRepTools
from OCP.gp import gp_Pnt
from OCP.TopoDS import TopoDS_Compound

from meshpipeline.cad.repair_v4.verdict import faithful_kernel, valid_kernel
from meshpipeline.cad.repair_v4.verdict.shape_digest import WorkerMemo, digest


def _box(x: float, size: float = 10.0):
    return BRepPrimAPI_MakeBox(gp_Pnt(x, 0.0, 0.0), size, size, size).Shape()


def _compound(*shapes):
    comp = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(comp)
    for shape in shapes:
        builder.Add(comp, shape)
    return comp


def _write(shape, path: Path) -> str:
    assert BRepTools.Write_s(shape, str(path))
    return str(path)


@pytest.fixture(autouse=True)
def fresh_memos(monkeypatch):
    monkeypatch.setattr(valid_kernel, "SELF_INTERSECTS", WorkerMemo(4096))
    monkeypatch.setattr(faithful_kernel, "_FACTS", WorkerMemo(16))


def test_the_digest_separates_one_ulp_and_matches_an_identical_rebuild() -> None:
    assert digest(_box(0.0)) == digest(_box(0.0))
    assert digest(_box(0.0)) != digest(_box(0.0, 10.0 + 2**-49))


def test_an_untouched_body_is_not_analysed_again_and_the_report_is_unchanged(tmp_path) -> None:
    a, b = _box(0.0), _box(50.0)
    first = valid_kernel.check_shape(_write(_compound(a, b), tmp_path / "first.brep"))
    assert valid_kernel.SELF_INTERSECTS.misses == 2 and valid_kernel.SELF_INTERSECTS.hits == 0
    changed = _box(50.0, 12.0)  # one body edited, the other written back bit for bit
    second = valid_kernel.check_shape(_write(_compound(a, changed), tmp_path / "second.brep"))
    assert valid_kernel.SELF_INTERSECTS.hits == 1 and valid_kernel.SELF_INTERSECTS.misses == 3
    fresh = WorkerMemo(4096)
    valid_kernel.SELF_INTERSECTS = fresh
    assert valid_kernel.check_shape(str(tmp_path / "second.brep")) == second
    assert first["hard"] == second["hard"] == []


def test_a_body_checked_twice_is_analysed_once_with_the_same_answer(tmp_path) -> None:
    fused = BRepAlgoAPI_Fuse(_box(0.0), _box(5.0)).Shape()  # a sound fused solid
    path = _write(_compound(fused), tmp_path / "fused.brep")
    once, twice = valid_kernel.check_shape(path), valid_kernel.check_shape(path)
    assert once == twice and valid_kernel.SELF_INTERSECTS.hits >= 1


def test_layer_3_side_facts_are_reused_and_change_nothing(tmp_path) -> None:
    original = _write(_compound(_box(0.0), _box(50.0)), tmp_path / "original.brep")
    candidate = _write(_compound(_box(0.0), _box(50.0, 10.05)), tmp_path / "candidate.brep")
    payload = {"original_brep": original, "repaired_brep": candidate, "declared_added": [], "declared_removed": []}
    first = faithful_kernel._faithful_check(payload)
    misses = faithful_kernel._FACTS.misses
    second = faithful_kernel._faithful_check(payload)
    assert second == first and faithful_kernel._FACTS.hits >= 2 and faithful_kernel._FACTS.misses == misses
    faithful_kernel._FACTS = WorkerMemo(16)
    assert faithful_kernel._faithful_check(payload) == first


def test_the_geometry_key_survives_a_step_round_trip_and_moves_with_a_real_edit(tmp_path) -> None:
    """C16: the digest changes on a STEP round trip of unchanged geometry; `geometry_key` must not, and must still
    change when the geometry does."""
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Reader, STEPControl_Writer

    from meshpipeline.cad.repair_v4.verdict.shape_digest import geometry_key

    shape = _compound(_box(0.0), _box(20.0, size=5.0))
    writer = STEPControl_Writer()
    writer.Transfer(shape, STEPControl_AsIs)
    assert writer.Write(str(tmp_path / "rt.step")) == IFSelect_RetDone
    reader = STEPControl_Reader()
    assert reader.ReadFile(str(tmp_path / "rt.step")) == IFSelect_RetDone
    reader.TransferRoots()
    again = reader.OneShape()

    assert geometry_key(again) == geometry_key(shape)
    assert geometry_key(_compound(_box(0.0), _box(20.001, size=5.0))) != geometry_key(shape)  # moved 1 um
    assert geometry_key(_compound(_box(0.0), _box(20.0, size=5.001))) != geometry_key(shape)  # grown 1 um
