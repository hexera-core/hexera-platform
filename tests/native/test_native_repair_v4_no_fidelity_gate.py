# Responsibility: Prove on the REAL kernel that nothing refuses on fidelity any more (owner decision 2026-11-24, lane
#                 B12): fill_loop closes a loop of any size, cut_by cuts an overlap of any depth (and REPORTS the depth),
#                 fuse merges bodies, remove_small takes a width far over the old bound. Each result is valid; what
#                 moved is measured in the op's record.
# Boundaries: the native tier (OCCT in a worker). No verdict here: these are the ops' own outcomes.
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("OCP.BRepAlgoAPI", reason="the CAD kernel is a native-tier dependency")

from OCP.BRepCheck import BRepCheck_Analyzer  # noqa: E402
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SOLID  # noqa: E402
from OCP.TopExp import TopExp  # noqa: E402
from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape  # noqa: E402
from tests.native.test_native_repair_v4_ops_bodies import (  # noqa: E402
    _bodies,
    _box,
    _common_volume,
    _compound,
    _count,
    _read,
    _solids,
    _write,
)

from meshpipeline.cad.repair_v4.ops import REGISTRY  # noqa: E402
from meshpipeline.cad.repair_v4.ops.bodies import Conformalise  # noqa: E402
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool  # noqa: E402
from meshpipeline.cad.repair_v4.workspace import Workspace  # noqa: E402

TIMEOUT = 300.0


@pytest.fixture
def pool() -> Iterator[WorkerPool]:
    p = WorkerPool(1)
    try:
        yield p
    finally:
        p.close()


def _open_box(tmp_path: Path) -> str:
    """A 100 x 60 x 40 mm box with ONE whole face missing: five faces sewn into an open shell whose free loop spans
    ~117 mm of a ~124 mm diagonal - 94%, far over the old 5% fill cap."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing
    from OCP.TopExp import TopExp_Explorer

    box = _box(0, 0, 0, 100, 60, 40)
    sew = BRepBuilderAPI_Sewing(1e-6)
    ex = TopExp_Explorer(box, TopAbs_FACE)
    n = 0
    while ex.More():
        if n != 0:
            sew.Add(ex.Current())
        n += 1
        ex.Next()
    sew.Perform()
    return _write(sew.SewedShape(), tmp_path / "open_box.brep")


def _free_edge_ids(ws: Workspace) -> list[str]:
    shape = _read(ws.brep_path)
    anc = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, anc)
    free = {i for i in range(1, anc.Extent() + 1) if anc.FindFromIndex(i).Size() == 1}
    from OCP.TopTools import TopTools_IndexedMapOfShape
    edges = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_EDGE, edges)
    free_idx = {edges.FindIndex(anc.FindKey(i)) for i in free}
    return sorted(sid for sid in ws.ids if sid.startswith("E") and ws.indices[sid] in free_idx)


def test_fill_loop_closes_a_loop_of_any_size(pool: WorkerPool, tmp_path: Path) -> None:
    ws = Workspace.from_brep(pool, _open_box(tmp_path))
    loop = _free_edge_ids(ws)
    assert len(loop) == 4
    new, diff = REGISTRY["fill_loop"].apply(pool, ws, loop, {"continuity": "G0"}, timeout_s=TIMEOUT)
    assert "declined" not in diff.params, diff.params
    measured = diff.params["measured"]
    assert measured["span_ratio"] > 0.5, measured                     # was refused above 0.05
    assert diff.declared_added and new.brep_path != ws.brep_path


def test_cut_by_cuts_a_deep_overlap_and_reports_its_depth(pool: WorkerPool, tmp_path: Path) -> None:
    shape = _compound(_box(0, 0, 0, 100, 100, 100), _box(95, 0, 0, 100, 100, 100))   # a 5 mm-deep overlap
    ws = Workspace.from_brep(pool, _write(shape, tmp_path / "deep.brep"))
    keeper, other = _bodies(ws)
    new, diff = Conformalise().apply(pool, ws, [keeper, other], {"mode": f"cut_by:{keeper}"}, timeout_s=TIMEOUT)
    assert "declined" not in diff.params, diff.params                 # was exceeds_fidelity (depth 5 > bound 0.1)
    [overlap] = diff.params["measured"]["overlaps"]
    assert overlap["depth_mm"] == pytest.approx(5.0, rel=0.02) and overlap["bound_mm"] == pytest.approx(0.1)
    out = _read(new.brep_path)
    assert BRepCheck_Analyzer(out).IsValid() and _count(out, TopAbs_SOLID) == 2
    a, b = _solids(out)
    assert _common_volume(a, b) < 1e-6


def test_remove_small_accepts_a_width_far_over_the_old_bound(pool: WorkerPool, tmp_path: Path) -> None:
    """A 10 mm part's old bound was 0.01 mm; max_width_mm 5.0 used to decline as invalid_params. It may now find
    nothing to remove (no_change) - but it is never refused for the width."""
    ws = Workspace.from_brep(pool, _write(_box(0, 0, 0, 10, 10, 10), tmp_path / "small.brep"))
    face = sorted(s for s in ws.ids if s.startswith("F"))[0]
    try:
        _, diff = REGISTRY["remove_small"].apply(pool, ws, [face], {"max_width_mm": 15.0}, timeout_s=TIMEOUT)
        reason = str(diff.params.get("declined") or "")
    except Exception as exc:  # noqa: BLE001 - an op decline raised as OpDeclined carries its reason
        reason = str(exc)
    assert "invalid_params" not in reason and "outside (0," not in reason, reason
