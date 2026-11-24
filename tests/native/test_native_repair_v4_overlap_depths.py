# Responsibility: The evaluation can report the owner's overlap ruling "both ways" from rows: the kernel measures an
#                 input's body overlap with the same `_cut_depth` that `conformalise(cut_by)` declines on, both cuts, with
#                 the method and the fidelity bound.
# Boundaries: the native tier; real OCCT in a spawned worker.
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("OCP.BRepAlgoAPI", reason="the CAD kernel is a native-tier dependency")

from meshpipeline.cad.repair_v4.worker_pool import WorkerPool  # noqa: E402


@pytest.fixture
def pool() -> Iterator[WorkerPool]:
    p = WorkerPool(1)
    try:
        yield p
    finally:
        p.close()


def _two_boxes(path: Path, overlap: float) -> str:
    from OCP.BRep import BRep_Builder
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Pnt
    from OCP.TopoDS import TopoDS_Compound

    a = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 50.0, 40.0, 30.0).Shape()
    b = BRepPrimAPI_MakeBox(gp_Pnt(50.0 - overlap, 0, 0), 50.0, 40.0, 30.0).Shape()
    compound = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(compound)
    builder.Add(compound, a)
    builder.Add(compound, b)
    BRepTools.Write_s(compound, str(path))
    return str(path)


@pytest.mark.parametrize("overlap", [0.05, 4.0])
def test_both_cuts_of_a_slab_overlap_read_its_thickness_against_the_bound(pool: WorkerPool, tmp_path: Path,
                                                                          overlap: float) -> None:
    brep = _two_boxes(tmp_path / "two.brep", overlap)
    r = pool.call("v4_overlap_depths", {"brep_path": brep, "pairs": [[1, 2]]}, timeout_s=180)
    assert r.ok and r.value is not None, r.error
    (pair,) = r.value["pairs"]
    assert pair["bodies"] == ["S1", "S2"]
    for cut in ("a_by_b", "b_by_a"):
        assert pair[cut]["method"] == "measured"
        assert pair[cut]["depth_mm"] == pytest.approx(overlap, rel=1e-3)
    assert r.value["bound_mm"] == pytest.approx(min(0.1, 0.001 * r.value["diag_mm"]))
