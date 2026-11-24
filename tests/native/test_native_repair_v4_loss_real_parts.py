# Responsibility: the owner's catastrophic-loss rule (B22, `loss.check_loss`) on the FOUR REAL v4 parts it was ruled
#                 on: 0051_00510567's search_ai delivery (0.07% of the part, 8 bodies -> 4) MUST be refused; the
#                 genuine fuses 00611396 (14 bodies -> 1), 00571995 (2 -> 1) and 00602103 (5 -> 4) must NOT be.
# Boundaries: the native tier; real OCCT in a spawned worker. Lane B's own natives prove the measurement on
#             synthetic geometry; this proves the RULE on the parts that caused it. Fixtures: each file's v4 input
#             BREP and the BREP v4 delivered for it (`tests/fixtures/geometry`).
from __future__ import annotations

import gzip
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("OCP.BRepAlgoAPI", reason="the CAD kernel is a native-tier dependency")

from meshpipeline.cad.repair_v4.loss import check_loss  # noqa: E402
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool  # noqa: E402

GEOMETRY = Path(__file__).resolve().parents[1] / "fixtures" / "geometry"


@pytest.fixture(scope="module")
def pool() -> Iterator[WorkerPool]:
    p = WorkerPool(1)
    try:
        yield p
    finally:
        p.close()


def _brep(tmp_path: Path, name: str) -> str:
    out = tmp_path / name.removesuffix(".gz")
    out.write_bytes(gzip.decompress((GEOMETRY / name).read_bytes()))
    return str(out)


def test_the_0_07_percent_delivery_of_00510567_is_refused(pool: WorkerPool, tmp_path: Path) -> None:
    """v4 search_ai, sha 3e7c2a1197c3: volume x0.00069, 8 bodies -> 4. Valid and meshable, and worthless."""
    report = check_loss(pool, _brep(tmp_path, "abc_00510567_input.brep.gz"),
                        _brep(tmp_path, "abc_00510567_delivered.brep.gz"))
    assert report.measured, report.unmeasured_why
    assert report.catastrophic, report.row()
    assert report.bodies_total == 8 and set(report.lost_bodies) >= {"S1", "S2", "S5", "S8"}


@pytest.mark.xfail(strict=True, reason="B22 hole: the delivered BREP holds an INSIDE-OUT solid (-13.5 mm3), and a Common "
                   "against an inverted solid is its complement, so the part reads 0.697 kept (and bodies S3/S4/S6/S7 "
                   "1.0) on a delivery holding 0.07% of the part. 0051 is still refused, by its other four bodies only.")
def test_the_part_level_number_of_00510567_says_what_is_left(pool: WorkerPool, tmp_path: Path) -> None:
    report = check_loss(pool, _brep(tmp_path, "abc_00510567_input.brep.gz"),
                        _brep(tmp_path, "abc_00510567_delivered.brep.gz"))
    assert report.part_kept is not None and report.part_kept < 0.01, report.row()
    assert report.reason == "part_volume_lost"


@pytest.mark.parametrize("stem, bodies", [("abc_00611396", 14), ("00571995", 2), ("abc_00602103", 5)])
def test_a_genuine_fuse_of_real_bodies_is_not_refused(pool: WorkerPool, tmp_path: Path, stem: str, bodies: int) -> None:
    """Each delivered fewer bodies than its input (14 -> 1, 2 -> 1, 5 -> 4) at ~0.93-1.0x the volume: merged, not
    lost. A rule written on the body count would refuse all three."""
    report = check_loss(pool, _brep(tmp_path, f"{stem}_input.brep.gz"), _brep(tmp_path, f"{stem}_delivered.brep.gz"))
    assert report.measured, report.unmeasured_why
    assert not report.catastrophic, report.row()
    assert report.lost_bodies == () and report.bodies_total == bodies
    assert report.part_kept > 0.9 and report.kept_min > 0.9
