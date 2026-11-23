# Responsibility: On the real kernel, at scales decades apart, the deterministic sweep GENERATES a sew candidate and the
#                 kernel ACCEPTS it; the old absolute ladder is DECLINED (not failed) at both; a part with no window gets
#                 no sew, visibly. A factor-of-1000 slip is invisible on one part, so two parts are used.
# Boundaries: native tier (OCCT); the real worker pool, `ws_diagonal`, `op_sew`. No model, no network.
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("OCP.BRepAlgoAPI", reason="the CAD kernel is a native-tier dependency")

from meshpipeline.cad.repair_v4.ops import REGISTRY, OpDeclined  # noqa: E402
from meshpipeline.cad.repair_v4.search import DetGenerator, SearchState  # noqa: E402
from meshpipeline.cad.repair_v4.types import Site  # noqa: E402
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool  # noqa: E402
from meshpipeline.cad.repair_v4.workspace import Workspace  # noqa: E402

OLD_LADDER = [1e-4, 1e-3, 1e-2]  # the absolute mm the generator used to send on every part
TIMEOUT = 180


@pytest.fixture
def pool() -> Iterator[WorkerPool]:
    p = WorkerPool(1)
    try:
        yield p
    finally:
        p.close()


def _loose_box(edge_mm: float, path: Path) -> str:
    """A box of this edge as 6 exactly-coincident loose faces: an open shell a sew closes at any rung in the window."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS_Compound

    compound = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(compound)
    explorer = TopExp_Explorer(BRepPrimAPI_MakeBox(edge_mm, edge_mm, edge_mm).Shape(), TopAbs_FACE)
    while explorer.More():
        builder.Add(compound, BRepBuilderAPI_Copy(explorer.Current(), True).Shape())
        explorer.Next()
    assert BRepTools.Write_s(compound, str(path))
    return str(path)


def _state(ws: Workspace) -> SearchState:
    site = Site(id="S1", kind="open", entities=[], xyz=[], meshers_failing=[], dist_over_h=None, graph={},
                view_png=None)
    return SearchState(id="S0", ws=ws, verdict=None, score=(0, 0, 0.0, 0.0, 0), sites=[site], depth=0, parent=None,
                       via=None)


def _free_edges(path: str) -> int:
    from meshpipeline.cad.repair_v4.ops.shells import _free_edge_count
    from meshpipeline.cad.repair_v4.ws_kernel import read_brep

    return _free_edge_count(read_brep(path))


#: 1 mm (diag 1.73 mm, bound 1.7e-3) and 1 m (diag 1732 mm, bound 0.1, floor 1.7e-3): three decades apart
@pytest.mark.parametrize("edge_mm", [1.0, 1000.0])
def test_a_sew_candidate_is_generated_and_accepted_at_each_scale(pool: WorkerPool, tmp_path: Path, edge_mm: float) -> None:
    ws = Workspace.from_brep(pool, _loose_box(edge_mm, tmp_path / "loose.brep"))
    before = _free_edges(ws.brep_path)
    assert before > 0

    gen = DetGenerator()
    gen.bind_pool(pool)
    sews = [c for c in gen.propose(_state(ws), 50) if c.op == "sew"]
    assert len(sews) == 1, f"no sew candidate for a {edge_mm} mm part; notes {gen.notes}"

    new, diff = REGISTRY["sew"].apply(pool, ws.fork(), list(sews[0].targets), dict(sews[0].params), timeout_s=TIMEOUT)
    assert diff.op == "sew"  # ACCEPTED: the kernel took the call, sewed, and kept it
    assert _free_edges(new.brep_path) < before


@pytest.mark.parametrize("edge_mm", [1.0, 1000.0])
def test_the_old_absolute_ladder_is_declined_not_failed_at_both_scales(pool: WorkerPool, tmp_path: Path, edge_mm: float) -> None:
    ws = Workspace.from_brep(pool, _loose_box(edge_mm, tmp_path / "loose.brep"))
    with pytest.raises(OpDeclined, match="must lie in"):  # OpFailed would not match: it is not a subclass
        REGISTRY["sew"].apply(pool, ws.fork(), [], {"tol_ladder": OLD_LADDER}, timeout_s=TIMEOUT)


def test_a_part_whose_window_is_empty_gets_no_sew_and_the_sweep_says_so(pool: WorkerPool, tmp_path: Path) -> None:
    ws = Workspace.from_brep(pool, _loose_box(200_000.0, tmp_path / "huge.brep"))  # a 200 m box: floor > 0.1 mm cap
    gen = DetGenerator()
    gen.bind_pool(pool)
    got = gen.propose(_state(ws), 50)
    assert "sew" not in {c.op for c in got}
    assert any("sew not proposed" in n for n in gen.notes), gen.notes
