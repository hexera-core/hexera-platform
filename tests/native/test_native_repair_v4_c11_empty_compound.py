# Responsibility: Lane C11 - a body op never leaves an EMPTY sub-compound behind. XCAF reads nest every solid one compound
#                 down; `_rebuild` used to re-add that compound after dropping all of its solids, and netgen_occ cannot
#                 read a STEP holding an empty compound ('Bnd_Box is void', `step_unreadable`). Measured on jig: cut_by
#                 and fuse of two overlapping boxes passed 3 of 4 members; the same output without the empty compound
#                 (same TShapes, same ids) passed 4 of 4 and was CLEAN. Pins the output form, the ids, and that verdict.
# Boundaries: the native tier; real OCCT in spawned workers, the real full panel (all four members).
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("OCP.BRepAlgoAPI", reason="the CAD kernel is a native-tier dependency")

from meshpipeline.cad.repair_v4 import search as S  # noqa: E402
from meshpipeline.cad.repair_v4.ops import REGISTRY  # noqa: E402
from meshpipeline.cad.repair_v4.verdict.assemble import verdict  # noqa: E402
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool  # noqa: E402
from meshpipeline.cad.repair_v4.workspace import Workspace  # noqa: E402

TIMEOUT = 600


@pytest.fixture
def pool() -> Iterator[WorkerPool]:
    p = WorkerPool(2)
    try:
        yield p
    finally:
        p.close()


def _two_overlapping_boxes(path: Path) -> str:
    """Two 50x40x30 boxes overlapping by 0.5 mm, written as STEP (the form a real input arrives in)."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    from OCP.TopoDS import TopoDS_Compound

    comp = TopoDS_Compound()
    b = BRep_Builder()
    b.MakeCompound(comp)
    b.Add(comp, BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 50, 40, 30).Shape())
    b.Add(comp, BRepPrimAPI_MakeBox(gp_Pnt(49.5, 0, 0), 50, 40, 30).Shape())
    w = STEPControl_Writer()
    w.Transfer(comp, STEPControl_AsIs)
    w.Write(str(path))
    return str(path)


def _read(path: str) -> Any:
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape

    shape = TopoDS_Shape()
    BRepTools.Read_s(shape, path, BRep_Builder())
    return shape


def _empty_compounds(shape: Any) -> int:
    from OCP.TopAbs import TopAbs_COMPOUND
    from OCP.TopoDS import TopoDS_Iterator

    it = TopoDS_Iterator(shape)
    if shape.ShapeType() == TopAbs_COMPOUND and not it.More():
        return 1
    n = 0
    while it.More():
        if it.Value().ShapeType() == TopAbs_COMPOUND:
            n += _empty_compounds(it.Value())
        it.Next()
    return n


def _input(pool: WorkerPool, tmp_path: Path) -> tuple[Workspace, str]:
    step = _two_overlapping_boxes(tmp_path / "two.step")
    read = pool.call("read_step_xcaf", {"path": step, "brep_out": str(tmp_path / "in.brep")}, timeout_s=TIMEOUT)
    assert read.ok, read.error
    ws = Workspace.from_brep(pool, read.value["brep"], read.value["labels"])
    # the precondition that made the bug: XCAF nests both solids in ONE sub-compound, so dropping both empties it
    from OCP.TopoDS import TopoDS_Iterator
    top = TopoDS_Iterator(_read(ws.brep_path))
    assert top.Value().ShapeType().name == "TopAbs_COMPOUND", "the fixture must nest its solids like a real read"
    return ws, step


@pytest.mark.parametrize("op, params", [("conformalise", {"mode": "cut_by:B1"}),
                                        ("conformalise", {"mode": "cut_by:B2"}),
                                        ("fuse", {})])
def test_a_body_op_on_a_nested_part_ends_clean_and_leaves_no_empty_compound(pool: WorkerPool, tmp_path: Path,
                                                                            op: str, params: dict) -> None:
    ws, step = _input(pool, tmp_path)
    before = S._judged(verdict, pool, ws, step)
    assert not before.valid.ok and [f.code for f in before.valid.hard] == ["body_overlap"]

    new, diff = REGISTRY[op].apply(pool, ws, ["B1", "B2"], params, timeout_s=TIMEOUT)
    assert diff.params.get("declined") is None, diff.params
    assert _empty_compounds(_read(new.brep_path)) == 0

    after = S._judged(verdict, pool, new, step)
    failing = [(m.name, m.status, m.error_kind, m.message[:120]) for m in after.panel if m.status != "pass"]
    assert after.valid.ok and failing == [] and len(after.panel) == 4, failing
    assert after.clean


def test_skipping_the_empty_compound_keeps_every_id_the_op_did_not_touch(pool: WorkerPool, tmp_path: Path) -> None:
    """A third box, far away and in its OWN sub-compound, must come out as the very same TShape with the same ids."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Pnt
    from OCP.TopoDS import TopoDS_Compound

    def comp(*shapes: Any) -> Any:
        c = TopoDS_Compound()
        b = BRep_Builder()
        b.MakeCompound(c)
        for s in shapes:
            b.Add(c, s)
        return c

    far = BRepPrimAPI_MakeBox(gp_Pnt(500, 0, 0), 10, 10, 10).Shape()
    part = comp(comp(BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 50, 40, 30).Shape(),
                     BRepPrimAPI_MakeBox(gp_Pnt(49.5, 0, 0), 50, 40, 30).Shape()), comp(far))
    path = str(tmp_path / "three.brep")
    BRepTools.Write_s(part, path)
    ws = Workspace.from_brep(pool, path)
    pair = sorted((s for s in ws.ids if s[0] == "B"), key=lambda s: int(s[1:]))[:2]
    new, diff = REGISTRY["conformalise"].apply(pool, ws, pair, {"mode": f"cut_by:{pair[0]}"}, timeout_s=TIMEOUT)
    assert diff.params.get("declined") is None, diff.params
    out = _read(new.brep_path)
    assert _empty_compounds(out) == 0
    untouched = [sid for sid, fp in ws.ids.items() if new.ids.get(sid) == fp]
    assert "B3" in untouched and len([s for s in untouched if s[0] == "F"]) >= 6  # the far box, whole


def test_rebuild_drops_only_the_emptied_compound_and_keeps_the_same_tshapes() -> None:
    """In the kernel itself: dropping both solids of one sub-compound removes THAT compound; the untouched solid is
    re-added as the SAME TShape (what keeps its ids), and the replacement is added at the top."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS_Compound, TopoDS_Iterator
    from OCP.TopTools import TopTools_IndexedMapOfShape

    from meshpipeline.cad.repair_v4.body_kernel import _rebuild

    def comp(*shapes: Any) -> Any:
        c = TopoDS_Compound()
        b = BRep_Builder()
        b.MakeCompound(c)
        for s in shapes:
            b.Add(c, s)
        return c

    a, b = BRepPrimAPI_MakeBox(10, 10, 10).Shape(), BRepPrimAPI_MakeBox(gp_Pnt(5, 0, 0), 10, 10, 10).Shape()
    far = BRepPrimAPI_MakeBox(gp_Pnt(100, 0, 0), 1, 1, 1).Shape()
    repl = BRepPrimAPI_MakeBox(gp_Pnt(0, 50, 0), 2, 2, 2).Shape()
    drop = TopTools_IndexedMapOfShape()
    drop.Add(a)
    drop.Add(b)
    out = _rebuild(comp(comp(a, b), comp(far)), drop, [repl])
    assert _empty_compounds(out) == 0
    kids = []
    it = TopoDS_Iterator(out)
    while it.More():
        kids.append(it.Value())
        it.Next()
    assert len(kids) == 2  # the far box's own compound, then the replacement; the emptied compound is gone
    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(out, TopAbs_SOLID, m)
    solids = [m.FindKey(i) for i in range(1, m.Extent() + 1)]
    assert any(s.IsPartner(far) for s in solids) and any(s.IsPartner(repl) for s in solids)  # same TShape
    assert not any(s.IsPartner(a) or s.IsPartner(b) for s in solids)
