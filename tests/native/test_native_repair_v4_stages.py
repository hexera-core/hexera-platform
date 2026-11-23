# Responsibility: Prove the staged healer on the REAL kernel and the REAL cheap checker: an unsewn
#                 box comes back as a valid, faithful solid; a clean part's score does not drop;
#                 a stage that really damages the part is rolled back by the real verdict.
# Boundaries: the native tier. Layers 1 and 3 of `cheap_verdict` are the real ones. Layer 2 is a
#             STAND-IN `GmshDefault` (below): `verdict/gmsh_members.py` (Tasks 3/4) is not in this
#             branch, so `default_members` cannot import. It meshes the STEP with real gmsh and
#             reports pass/fail and the real minimum SICN. Drop it once the panel merges.
#             The unsewn box is authored here (6 copied faces, two shifted 0.02 mm, i.e. inside
#             the sewing bound), not by devtools' injector.
from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("OCP.BRepAlgoAPI", reason="the CAD kernel is a native-tier dependency")

from OCP.BRep import BRep_Builder  # noqa: E402
from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy, BRepBuilderAPI_Transform  # noqa: E402
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet  # noqa: E402
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder  # noqa: E402
from OCP.BRepTools import BRepTools, BRepTools_History  # noqa: E402
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec  # noqa: E402
from OCP.ShapeBuild import ShapeBuild_ReShape  # noqa: E402
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SOLID  # noqa: E402
from OCP.TopExp import TopExp  # noqa: E402
from OCP.TopoDS import TopoDS, TopoDS_Shell  # noqa: E402
from OCP.TopTools import TopTools_IndexedMapOfShape  # noqa: E402

from meshpipeline.cad.repair_v4 import ops, stages, ws_kernel  # noqa: E402
from meshpipeline.cad.repair_v4.ops import OpFailed  # noqa: E402
from meshpipeline.cad.repair_v4.verdict.assemble import cheap_verdict  # noqa: E402
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool  # noqa: E402
from meshpipeline.cad.repair_v4.workspace import Workspace, apply_history  # noqa: E402


@pytest.fixture(scope="module")
def pool() -> Iterator[WorkerPool]:
    p = WorkerPool(1, start_timeout_s=180)
    try:
        yield p
    finally:
        p.close()


def _map(shape: Any, kind: Any) -> Any:
    out = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, kind, out)
    return out


def _write(shape: Any, path: Path) -> str:
    assert BRepTools.Write_s(shape, str(path))
    return str(path)


def _unsewn_box(path: Path) -> str:
    box = BRepPrimAPI_MakeBox(40.0, 30.0, 20.0).Shape()
    faces = _map(box, TopAbs_FACE)
    shell = TopoDS_Shell()
    builder = BRep_Builder()
    builder.MakeShell(shell)
    for i in range(1, faces.Extent() + 1):
        piece = BRepBuilderAPI_Copy(faces.FindKey(i)).Shape()  # owns its edges: nothing is shared
        if i in (2, 5):
            trsf = gp_Trsf()
            trsf.SetTranslation(gp_Vec(0.02, -0.015, 0.01))  # inside the sewing bound (~0.054 mm)
            piece = BRepBuilderAPI_Transform(piece, trsf, True).Shape()
        builder.Add(shell, piece)
    return _write(shell, path)


def _clean_part(path: Path) -> str:
    box = BRepPrimAPI_MakeBox(40.0, 30.0, 20.0).Shape()
    fillet = BRepFilletAPI_MakeFillet(box)
    edges = _map(box, TopAbs_EDGE)
    for i in range(1, edges.Extent() + 1):
        edge = TopoDS.Edge_s(edges.FindKey(i))
        from OCP.BRep import BRep_Tool
        a, b = BRep_Tool.Pnt_s(TopExp.FirstVertex_s(edge)), BRep_Tool.Pnt_s(TopExp.LastVertex_s(edge))
        if abs(a.X() - b.X()) < 1e-9 and abs(a.Y() - b.Y()) < 1e-9:
            fillet.Add(3.0, edge)
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    hole = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(20.0, 15.0, -1.0), gp_Dir(0, 0, 1)), 4.0, 22.0).Shape()
    return _write(BRepAlgoAPI_Cut(fillet.Shape(), hole).Shape(), path)


@dataclass(frozen=True)
class _Result:
    name: str
    status: str
    min_sicn: float | None
    located: list = field(default_factory=list)
    error_kind: str | None = None


class _StandInGmshDefault:
    name = "GmshDefault"
    config: dict = {}

    def run(self, pool: WorkerPool, step_path: str, *, timeout_s: float) -> _Result:
        import gmsh

        gmsh.initialize()
        try:
            gmsh.option.setNumber("General.Terminal", 0)
            gmsh.open(step_path)
            gmsh.model.mesh.generate(3)
            tags, _ = gmsh.model.mesh.getElementsByType(4)
            if len(tags) == 0:
                return _Result(self.name, "fail", None, error_kind="no_volume_mesh")
            sicn = gmsh.model.mesh.getElementQualities(list(tags), "minSICN")
            return _Result(self.name, "pass", float(min(sicn)))
        except Exception as why:  # noqa: BLE001 - the mesher refusing the file is the file failing
            return _Result(self.name, "fail", None, error_kind=type(why).__name__)
        finally:
            gmsh.finalize()


def _run_panel(pool: WorkerPool, step: str, members: list, *, timeout_s: float) -> list:
    return [m.run(pool, step, timeout_s=timeout_s) for m in members]


def _stand_in_cheap_verdict(pool: WorkerPool, step: str, original: str, **kw: Any) -> Any:
    return cheap_verdict(pool, step, original, members=[_StandInGmshDefault()], run_panel=_run_panel)


@pytest.fixture(autouse=True)
def _panel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stages, "cheap_verdict", _stand_in_cheap_verdict)


def _judge(pool: WorkerPool, ws: Workspace) -> tuple:
    return stages._judge(pool, ws)


def _counts(path: str) -> tuple[int, int]:
    shape = ws_kernel.read_brep(path)
    return _map(shape, TopAbs_SOLID).Extent(), _map(shape, TopAbs_FACE).Extent()


def test_unsewn_box_comes_back_clean(pool: WorkerPool, tmp_path: Path) -> None:
    ws = Workspace.from_brep(pool, _unsewn_box(tmp_path / "unsewn.brep"))
    assert _counts(ws.brep_path) == (0, 6)
    before = _judge(pool, ws)
    assert before[0] == 0, before  # the damage is real: not valid
    started = time.time()
    res = stages.run_staged(pool, ws)
    print(f"\nkept={res.kept} rolled_back={res.rolled_back} inconclusive={res.inconclusive} "
          f"skipped={res.skipped} seconds={time.time() - started:.0f}")
    after = _judge(pool, res.ws)
    assert after > before and after[0] == 1, (before, after)
    # Layer 1: the healed file is valid and one solid. Layer 3 compares it with the DAMAGED input,
    # which has no solid, so volume and body count legitimately differ (6 loose faces -> 1 body);
    # nothing else may: same area, no deviation. (That is why `score`'s first key stays 0 here.)
    v = cheap_verdict(pool, stages.step_for(pool, res.ws.brep_path),
                      stages.step_for(pool, ws.original),
                      members=[_StandInGmshDefault()], run_panel=_run_panel)
    assert v.valid.ok and v.valid.solids == 1, v.valid
    assert v.faithful is not None
    assert set(v.faithful.reasons) <= {"volume_changed", "body_count_changed"}, v.faithful
    assert v.faithful.area_ratio == pytest.approx(1.0, abs=1e-3) and v.faithful.max_dev_mm == 0.0
    assert v.panel[0].status == "pass"
    assert "shells" in res.kept
    assert _counts(res.ws.brep_path)[0] == 1  # sew's solid loss was repaired by make_volume
    assert res.rolled_back == [] and res.inconclusive == []
    ops_run = [d.op for d in res.ws.history]
    assert ops_run.index("make_volume") == ops_run.index("sew") + 1, ops_run


def test_clean_control_part_does_not_lose_score(pool: WorkerPool, tmp_path: Path) -> None:
    ws = Workspace.from_brep(pool, _clean_part(tmp_path / "clean.brep"))
    before = _judge(pool, ws)
    assert before[0] == 1, before  # the control really is clean
    res = stages.run_staged(pool, ws)
    print(f"\nkept={res.kept} rolled_back={res.rolled_back} inconclusive={res.inconclusive} "
          f"skipped={res.skipped} faces {_counts(ws.brep_path)} -> {_counts(res.ws.brep_path)}")
    after = _judge(pool, res.ws)
    assert after >= before, (before, after)
    assert after[:2] == before[:2]
    assert after[3] >= before[3] - 1e-6  # no more deviation than the control started with
    assert res.inconclusive == []
    assert _counts(res.ws.brep_path)[0] == 1


class _DeleteAFace:
    """A REAL damaging op: removes the first face from the solid, so the part is no longer closed."""

    name = "to_canonical"
    params_schema: dict = {"type": "object"}

    def applies_to(self, site: Any) -> bool:
        return True

    def apply(self, pool: WorkerPool, ws: Workspace, targets: list[str], params: dict, *,
              timeout_s: float) -> tuple[Workspace, Any]:
        old = ws_kernel.read_brep(ws.brep_path)
        reshape = ShapeBuild_ReShape()
        reshape.Remove(_map(old, TopAbs_FACE).FindKey(1))
        new = reshape.Apply(old)
        out = str(Path(ws.brep_path).parent / "damaged.brep")
        ws_kernel.write_brep(new, out)
        try:
            record = ws_kernel.history_record(old, new, BRepTools_History(), op=self.name, params={},
                                              old_path=ws.brep_path, new_path=out,
                                              quantum=ws.quantum, seconds=0.0)
            fresh = apply_history(ws, record)
        except Exception as why:  # noqa: BLE001
            raise OpFailed("error", str(why)) from why
        return fresh, fresh.history[-1]


def test_a_stage_that_really_damages_the_part_is_rolled_back(
        pool: WorkerPool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = Workspace.from_brep(pool, _clean_part(tmp_path / "clean.brep"))
    before = _judge(pool, ws)
    assert before[0] == 1
    monkeypatch.setitem(ops.REGISTRY, "to_canonical", _DeleteAFace())
    res = stages.run_stage(pool, ws, "faces")
    damaged = Workspace.from_brep(pool, str(tmp_path / "damaged.brep"))
    assert _judge(pool, Workspace(brep_path=damaged.brep_path, ids=damaged.ids, labels={}, history=[],
                                  original=ws.brep_path, quantum=ws.quantum,
                                  indices=damaged.indices))[0] == 0  # the op DID break the file
    assert res.rolled_back == ["faces"] and res.kept == []
    assert res.verdicts["faces"] < before
    assert res.ws.brep_path == ws.brep_path
    assert _judge(pool, res.ws) == before


def _two_overlapping_solids(path: Path) -> str:
    """Two boxes that overlap by 10 mm, as two solids of one compound: the multi-solid overlap."""
    from OCP.TopoDS import TopoDS_Compound

    a = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 30.0, 30.0, 30.0).Shape()
    b = BRepPrimAPI_MakeBox(gp_Pnt(20, 0, 0), 30.0, 30.0, 30.0).Shape()
    comp, builder = TopoDS_Compound(), BRep_Builder()
    builder.MakeCompound(comp)
    builder.Add(comp, a)
    builder.Add(comp, b)
    return _write(comp, path)


def test_bodies_on_a_bulk_overlap_declines_and_changes_nothing(pool: WorkerPool, tmp_path: Path) -> None:
    ws = Workspace.from_brep(pool, _two_overlapping_solids(tmp_path / "two.brep"))
    assert _counts(ws.brep_path)[0] == 2
    res = stages.run_stage(pool, ws, "bodies")
    # conformalise(fragment) refuses a bulk overlap ('unresolved_overlap', 9000 mm3) with an
    # UNCHANGED workspace; the stage must say so instead of calling that a kept change.
    assert res.kept == [] and res.rolled_back == [] and res.ws is ws
    assert "unresolved_overlap" in res.skipped["bodies"], res.skipped


def _touching_solids(path: Path) -> str:
    """Two boxes sharing a PARTIALLY coincident face (the facet-overlap shape: no bulk overlap)."""
    from OCP.TopoDS import TopoDS_Compound

    a = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 30.0, 30.0, 30.0).Shape()
    b = BRepPrimAPI_MakeBox(gp_Pnt(30, 10, 0), 30.0, 30.0, 30.0).Shape()
    comp, builder = TopoDS_Compound(), BRep_Builder()
    builder.MakeCompound(comp)
    builder.Add(comp, a)
    builder.Add(comp, b)
    return _write(comp, path)


def test_bodies_on_facet_contact_does_not_trip_the_body_count_gate(pool: WorkerPool, tmp_path: Path) -> None:
    """The multi-solid shape conformalise CAN resolve. The solid count is unchanged (2 -> 2, faces
    12 -> 13), body_count_changed does not fire, and score key 0 stays 1, so the stage is judged on
    mesh quality alone. (On this part the shared-interface split lowers GmshDefault's min SICN
    from 0.328 to 0.318, so the transaction rule rolls it back: a correct rollback, not a gate bug.)"""
    ws = Workspace.from_brep(pool, _touching_solids(tmp_path / "touch.brep"))
    before = _judge(pool, ws)
    assert before[0] == 1
    stage = next(s for s in stages.STAGES if s.name == "bodies")
    candidate, diffs = stage.run(pool, ws, {})
    assert [d.op for d in diffs] == ["conformalise"]
    assert _counts(ws.brep_path) == (2, 12) and _counts(candidate.brep_path) == (2, 13)
    after = _judge(pool, candidate)
    assert after[0] == 1 and after[1] == before[1]
    res = stages.run_stage(pool, ws, "bodies")
    assert res.inconclusive == [] and res.verdicts["bodies"] == after
    assert (res.kept == ["bodies"]) == (after >= before)


class _DeleteAFaceDeclared(_DeleteAFace):
    """The same real face deletion, but it DECLARES the removal of that face."""

    def apply(self, pool: WorkerPool, ws: Workspace, targets: list[str], params: dict, *,
              timeout_s: float) -> tuple[Workspace, Any]:
        gone = next(s for s, i in ws.indices.items() if s[0] == "F" and i == 1)
        new, diff = super().apply(pool, ws, targets, params, timeout_s=timeout_s)
        diff.declared_removed = [gone]
        return new, diff


def test_declared_removal_reaches_layer_three(pool: WorkerPool, tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    from meshpipeline.cad.repair_v4.verdict.faithful import check_faithful

    ws = Workspace.from_brep(pool, _clean_part(tmp_path / "clean.brep"))
    new, _ = _DeleteAFaceDeclared().apply(pool, ws, [], {}, timeout_s=60)
    faces, _solids = stages.original_facts(pool, ws.original)
    declared = stages.declared_edits(new, faces)
    assert declared.removed == ["F1"] and declared.added == []
    undeclared = check_faithful(pool, ws.original, new.brep_path, declared_added=[], declared_removed=[])
    with_it = check_faithful(pool, ws.original, new.brep_path, declared_added=declared.added,
                             declared_removed=declared.removed)
    assert "deviation_exceeds_bound" in undeclared.reasons  # undeclared, the deletion is a deviation
    assert "deviation_exceeds_bound" not in with_it.reasons  # declared, it is exempt from it
    assert with_it.declared_removed == ["F1"]


def test_unsewn_healed_box_gates_on_validity_and_a_later_damage_is_rolled_back(
        pool: WorkerPool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = Workspace.from_brep(pool, _unsewn_box(tmp_path / "unsewn.brep"))
    healed = stages.run_staged(pool, ws).ws
    good = _judge(pool, healed)
    assert good[0] == 1  # the local gate: valid, and layer 3 says only volume/body count
    # The healed solid is now damaged by a real op while the ORIGINAL still has no solid: the
    # file is BRepCheck-invalid and key 0 of `score` is 0 either way, so only the local gate
    # catches it.
    monkeypatch.setitem(ops.REGISTRY, "to_canonical", _DeleteAFace())
    res = stages.run_stage(pool, healed, "faces")
    assert res.rolled_back == ["faces"] and res.kept == [] and res.ws is healed
    assert res.verdicts["faces"][0] == 0
