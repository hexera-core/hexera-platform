# Responsibility: Lane C7, on the real kernel: the owner's 2026-11-24 goal (clean = valid + panel; faithfulness reported,
#                 never a gate) made three body ops usable - `fuse` (the no-fuse rule is gone), `conformalise(cut_by)` at
#                 ANY depth (the depth is measured and reported, the victim may split or vanish), and `trim_unbounded`
#                 (Raymond Lee's Common with an enlarged part box, copied from raymond/cad-simplification devtools/simplify).
# Boundaries: the native tier; real OCCT in spawned workers through the ops' own `apply`.
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("OCP.BRepAlgoAPI", reason="the CAD kernel is a native-tier dependency")

from meshpipeline.cad.repair_v4.ops import REGISTRY  # noqa: E402
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool  # noqa: E402
from meshpipeline.cad.repair_v4.workspace import Workspace  # noqa: E402

TIMEOUT = 300


@pytest.fixture
def pool() -> Iterator[WorkerPool]:
    p = WorkerPool(1)
    try:
        yield p
    finally:
        p.close()


def _write(shapes: list[Any], path: Path) -> str:
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Compound

    compound = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(compound)
    for s in shapes:
        builder.Add(compound, s)
    BRepTools.Write_s(compound, str(path))
    return str(path)


def _box(x: float, y: float, z: float, dx: float, dy: float, dz: float) -> Any:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    return BRepPrimAPI_MakeBox(gp_Pnt(x, y, z), dx, dy, dz).Shape()


def _read(path: str) -> Any:
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape

    shape = TopoDS_Shape()
    BRepTools.Read_s(shape, path, BRep_Builder())
    return shape


def _solids(shape: Any) -> int:
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_SOLID, m)
    return m.Extent()


def _valid(shape: Any) -> bool:
    from OCP.BRepCheck import BRepCheck_Analyzer

    return bool(BRepCheck_Analyzer(shape).IsValid())


def _volume(shape: Any) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return props.Mass()


def _bodies(ws: Workspace) -> list[str]:
    return sorted((s for s in ws.ids if s[0] == "B"), key=lambda s: int(s[1:]))


# --- fuse ---------------------------------------------------------------------------------------------------------


def test_fuse_merges_two_overlapping_bodies_into_one_valid_solid(pool: WorkerPool, tmp_path: Path) -> None:
    ws = Workspace.from_brep(pool, _write([_box(0, 0, 0, 50, 40, 30), _box(46, 0, 0, 50, 40, 30)], tmp_path / "a.brep"))
    new, diff = REGISTRY["fuse"].apply(pool, ws, _bodies(ws), {}, timeout_s=TIMEOUT)
    out = _read(new.brep_path)
    assert diff.params.get("declined") is None, diff.params
    assert _solids(out) == 1 and _valid(out)
    assert _volume(out) == pytest.approx(96 * 40 * 30, rel=1e-6)
    assert diff.params["measured"]["solids_before"] == 2 and diff.params["measured"]["solids_after"] == 1


def test_fuse_absorbs_a_body_inside_another(pool: WorkerPool, tmp_path: Path) -> None:
    ws = Workspace.from_brep(pool, _write([_box(0, 0, 0, 50, 50, 50), _box(10, 10, 10, 5, 5, 5)], tmp_path / "b.brep"))
    new, diff = REGISTRY["fuse"].apply(pool, ws, _bodies(ws), {}, timeout_s=TIMEOUT)
    out = _read(new.brep_path)
    assert _solids(out) == 1 and _valid(out) and _volume(out) == pytest.approx(50 ** 3, rel=1e-6)


def test_fuse_of_bodies_that_do_not_touch_declines_instead_of_reporting_progress(pool: WorkerPool,
                                                                                 tmp_path: Path) -> None:
    ws = Workspace.from_brep(pool, _write([_box(0, 0, 0, 10, 10, 10), _box(50, 0, 0, 10, 10, 10)], tmp_path / "c.brep"))
    new, diff = REGISTRY["fuse"].apply(pool, ws, _bodies(ws), {}, timeout_s=TIMEOUT)
    assert diff.params["declined"] == "nothing_to_fuse" and new is ws


# --- cut_by at any depth ------------------------------------------------------------------------------------------


def test_cut_by_resolves_a_4_mm_overlap_and_reports_its_depth(pool: WorkerPool, tmp_path: Path) -> None:
    """4 mm is 40x the old fidelity bound (0.1 mm): the cut used to decline `exceeds_fidelity`."""
    ws = Workspace.from_brep(pool, _write([_box(0, 0, 0, 50, 40, 30), _box(46, 0, 0, 50, 40, 30)], tmp_path / "d.brep"))
    keep = _bodies(ws)[0]
    new, diff = REGISTRY["conformalise"].apply(pool, ws, _bodies(ws), {"mode": f"cut_by:{keep}"}, timeout_s=TIMEOUT)
    out = _read(new.brep_path)
    assert diff.params.get("declined") is None, diff.params
    assert _solids(out) == 2 and _valid(out)
    assert _volume(out) == pytest.approx(96 * 40 * 30, rel=1e-6)  # the overlap is counted once, in the keeper
    (overlap,) = diff.params["measured"]["overlaps"]
    assert overlap["depth_mm"] == pytest.approx(4.0, rel=1e-3) and overlap["method"] == "measured"
    assert overlap["depth_mm"] > overlap["bound_mm"]


def test_cut_by_may_split_its_victim_and_keeps_every_piece(pool: WorkerPool, tmp_path: Path) -> None:
    """A plate driven right through a block: cut by the block, the plate leaves in two pieces. The old op declined
    `changes_body_count`; the count is now layer 3's to report."""
    block, plate = _box(0, 0, 0, 20, 20, 20), _box(-10, 5, 8, 40, 10, 4)
    ws = Workspace.from_brep(pool, _write([block, plate], tmp_path / "e.brep"))
    keep = _bodies(ws)[0]
    new, diff = REGISTRY["conformalise"].apply(pool, ws, _bodies(ws), {"mode": f"cut_by:{keep}"}, timeout_s=TIMEOUT)
    out = _read(new.brep_path)
    assert diff.params.get("declined") is None, diff.params
    assert _solids(out) == 3 and _valid(out)
    assert diff.params["measured"]["solids_after"] == 3


# --- trim_unbounded -----------------------------------------------------------------------------------------------


def _infinite_plane_face() -> Any:
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.gp import gp_Dir, gp_Pln, gp_Pnt

    return BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(0, 0, 5), gp_Dir(0, 0, 1))).Face()  # no wire: infinite


def test_trim_unbounded_bounds_an_infinite_surface_to_the_parts_extent(pool: WorkerPool, tmp_path: Path) -> None:
    from meshpipeline.cad.repair_v4.ws_kernel import diagonal_of

    raw = [_box(0, 0, 0, 20, 10, 10), _infinite_plane_face()]
    path = _write(raw, tmp_path / "f.brep")
    assert diagonal_of(_read(path)) > 1e9, "the fixture must be unbounded for the test to mean anything"
    ws = Workspace.from_brep(pool, path)
    new, diff = REGISTRY["trim_unbounded"].apply(pool, ws, [], {"pad_fraction": 0.01}, timeout_s=TIMEOUT)
    assert diff.params.get("declined") is None, diff.params
    out = _read(new.brep_path)
    d = diagonal_of(out)
    box_diag = (20 ** 2 + 10 ** 2 + 10 ** 2) ** 0.5
    assert box_diag <= d <= box_diag * 1.03 + 1e-6  # the part's own extent plus 1% of its diagonal each side
    assert _solids(out) == 1 and diff.params["measured"]["unbounded_faces"] == 1


def test_trim_unbounded_declines_a_part_with_nothing_unbounded(pool: WorkerPool, tmp_path: Path) -> None:
    ws = Workspace.from_brep(pool, _write([_box(0, 0, 0, 20, 10, 10)], tmp_path / "g.brep"))
    new, diff = REGISTRY["trim_unbounded"].apply(pool, ws, [], {"pad_fraction": 0.01}, timeout_s=TIMEOUT)
    assert diff.params["declined"] == "nothing_unbounded" and new is ws
