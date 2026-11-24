# Responsibility: B22's measurement works on REAL geometry - `v4_volume_loss` reads what an output kept of the
#                 input part, per input body, and a FUSE reads as no loss at all while a deletion reads as one.
# Boundaries: the native tier; real OCCT in a spawned worker. The 10% threshold and the refusal are loss.py's,
#             tested hermetically (tests/unit/cad/repair_v4/test_b22_catastrophic_loss.py); this proves the numbers.
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("OCP.BRepAlgoAPI", reason="the CAD kernel is a native-tier dependency")

from meshpipeline.cad.repair_v4.loss import check_loss  # noqa: E402
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool  # noqa: E402


@pytest.fixture
def pool() -> Iterator[WorkerPool]:
    p = WorkerPool(1)
    try:
        yield p
    finally:
        p.close()


def _box(x: float, y: float, z: float, dx: float, dy: float, dz: float) -> Any:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    return BRepPrimAPI_MakeBox(gp_Pnt(x, y, z), dx, dy, dz).Shape()


def _write(path: Path, shapes: list) -> str:
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Compound

    compound = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(compound)
    for shape in shapes:
        builder.Add(compound, shape)
    BRepTools.Write_s(compound, str(path))
    return str(path)


def _two_overlapping() -> list:
    """Two 50x40x30 boxes sharing a 4 mm slab - the overlap repairs this engine exists for."""
    return [_box(0, 0, 0, 50, 40, 30), _box(46, 0, 0, 50, 40, 30)]


def test_an_unchanged_output_keeps_the_whole_part(pool: WorkerPool, tmp_path: Path) -> None:
    original = _write(tmp_path / "in.brep", _two_overlapping())
    report = check_loss(pool, original, _write(tmp_path / "same.brep", _two_overlapping()))
    assert report.measured and not report.catastrophic
    assert report.part_kept == pytest.approx(1.0, rel=1e-6)
    assert report.bodies_measured == 2 and report.kept_min == pytest.approx(1.0, rel=1e-6)


def test_a_FUSE_of_the_two_bodies_loses_NOTHING_though_the_volume_ratio_falls(pool: WorkerPool,
                                                                             tmp_path: Path) -> None:
    """The real shape of the owner's "a fuse is not a loss": one solid instead of two, every input body's volume
    still exactly where it was, and a total volume BELOW the input's because the shared slab was counted twice."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse

    a, b = _two_overlapping()
    fused = BRepAlgoAPI_Fuse(a, b).Shape()
    original = _write(tmp_path / "in.brep", [a, b])
    report = check_loss(pool, original, _write(tmp_path / "fused.brep", [fused]))

    assert report.measured and not report.catastrophic and report.reason is None
    assert report.lost_bodies == ()
    assert report.part_kept == pytest.approx(1.0, rel=1e-6)
    assert report.kept_min == pytest.approx(1.0, rel=1e-6)
    # the body count dropped 2 -> 1 and the volume ratio is under 1: neither is what the rule read
    assert report.volume_ratio is not None and 0.9 < report.volume_ratio < 1.0


def test_a_DELETED_body_is_measured_as_a_lost_body(pool: WorkerPool, tmp_path: Path) -> None:
    a, b = _two_overlapping()
    original = _write(tmp_path / "in.brep", [a, b])
    report = check_loss(pool, original, _write(tmp_path / "one.brep", [a]))

    assert report.catastrophic and report.reason == "body_lost"
    assert report.lost_bodies == ("S2",)
    # the part is most of the way there, which is exactly why rule 2 exists
    assert report.part_kept is not None and report.part_kept > 0.5


def test_a_FRAGMENT_of_the_part_is_measured_as_the_part_being_lost(pool: WorkerPool, tmp_path: Path) -> None:
    """v4's sha 3e7c2a1197c3 shape: a valid, meshable output holding a fraction of a percent of the input."""
    a, b = _two_overlapping()
    original = _write(tmp_path / "in.brep", [a, b])
    sliver = _write(tmp_path / "sliver.brep", [_box(0, 0, 0, 5, 4, 3)])
    report = check_loss(pool, original, sliver)

    assert report.catastrophic and report.reason == "part_volume_lost"
    assert report.part_kept is not None and report.part_kept < 0.01


def test_a_SOLIDLESS_input_is_measured_and_refuses_nothing(pool: WorkerPool, tmp_path: Path) -> None:
    """A shell input has no volume to keep: a heal that makes it a solid must never be refused for it."""
    from OCP.TopAbs import TopAbs_SHELL
    from OCP.TopExp import TopExp_Explorer

    solid = _box(0, 0, 0, 50, 40, 30)
    explorer = TopExp_Explorer(solid, TopAbs_SHELL)
    shell = explorer.Current()
    original = _write(tmp_path / "shell.brep", [shell])
    report = check_loss(pool, original, _write(tmp_path / "solid.brep", [solid]))
    assert report.measured and report.solidless and not report.catastrophic
