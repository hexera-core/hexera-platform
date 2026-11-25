"""D14 on the real kernel: Raymond Lee's recogniser and removal, ported into the v4 defeature op.

Shapes are built here from OCCT primitives, so every claim is about a part whose features are known by construction.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("OCP.BRepAlgoAPI", reason="the CAD kernel is a native-tier dependency")

from meshpipeline.cad.repair_v4.ops import REGISTRY  # noqa: E402
from meshpipeline.cad.repair_v4.ops import defeature as D  # noqa: E402
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool  # noqa: E402
from meshpipeline.cad.repair_v4.workspace import Workspace  # noqa: E402


@pytest.fixture
def pool() -> Iterator[WorkerPool]:
    p = WorkerPool(1)
    try:
        yield p
    finally:
        p.close()


def _box(x: float, y: float, z: float) -> Any:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    return BRepPrimAPI_MakeBox(x, y, z).Shape()


def _cyl(cx: float, cy: float, r: float, h: float) -> Any:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(cx, cy, -1.0), gp_Dir(0, 0, 1)), r, h + 2.0).Shape()


def _cut(a: Any, b: Any) -> Any:
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    return BRepAlgoAPI_Cut(a, b).Shape()


def _fuse(a: Any, b: Any) -> Any:
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
    return BRepAlgoAPI_Fuse(a, b).Shape()


def holed_and_filleted() -> Any:
    """A 60 x 40 x 20 block with a through hole (r 3) and one vertical edge filleted (r 1.5)."""
    from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    block = _cut(_box(60, 40, 20), _cyl(20, 20, 3.0, 20))
    fillet = BRepFilletAPI_MakeFillet(block)
    e = TopExp_Explorer(block, TopAbs_EDGE)
    while e.More():                      # the vertical edge at x=60, y=40
        edge = TopoDS.Edge_s(e.Current())
        from OCP.BRepAdaptor import BRepAdaptor_Curve
        c = BRepAdaptor_Curve(edge)
        p0, p1 = c.Value(c.FirstParameter()), c.Value(c.LastParameter())
        if abs(p0.X() - 60) < 1e-6 and abs(p1.X() - 60) < 1e-6 and abs(p0.Y() - 40) < 1e-6 and abs(p1.Y() - 40) < 1e-6:
            fillet.Add(1.5, edge)
            break
        e.Next()
    return fillet.Shape()


def slotted() -> Any:
    """A 60 x 40 x 20 block with a through slot whose end at x = 30 is a 180-degree concave round (r 5)."""
    tool = _fuse(_box(20, 10, 20).Moved(_loc(10, 15, 0)), _cyl(30, 20, 5.0, 20))
    return _cut(_box(60, 40, 20), tool)


def _loc(x: float, y: float, z: float) -> Any:
    from OCP.gp import gp_Trsf, gp_Vec
    from OCP.TopLoc import TopLoc_Location
    t = gp_Trsf()
    t.SetTranslation(gp_Vec(x, y, z))
    return TopLoc_Location(t)


def _ws(pool: WorkerPool, shape: Any, tmp_path: Path, name: str) -> Workspace:
    from OCP.BRepTools import BRepTools
    path = tmp_path / f"{name}.brep"
    BRepTools.Write_s(shape, str(path))
    return Workspace.from_brep(pool, str(path))


# ------------------------------------------------------------------ the recogniser (his rules)

def test_the_recogniser_finds_the_hole_and_the_blend() -> None:
    features, diag = D.recognise(holed_and_filleted())
    kinds = sorted(f["kind"] for f in features)
    assert kinds == ["blend", "hole"], features
    assert diag > 70 and all(f["removable"] for f in features)


def test_a_180_degree_concave_round_is_a_slot_end_never_removable() -> None:
    features, _ = D.recognise(slotted())
    ends = [f for f in features if f["kind"] == "slot_end"]
    assert len(ends) == 1 and ends[0]["removable"] is False
    assert D.defeature_rows([{**f, "face_ids": [f"F{i}" for i in f["faces"]]} for f in features]) == [] or all(
        "slot_end" not in str(r) for r in D.defeature_rows([{**f, "face_ids": [f"F{i}" for i in f["faces"]]}
                                                             for f in features]))


def test_why_slot_ends_are_excluded_removing_one_is_never_just_the_end() -> None:
    """His DEVLOG (NIST nist_ftc_09): BRepAlgoAPI_Defeaturing handed 180-degree concave rounds back UNCHANGED. On this
    synthetic through-slot (OCCT 7.8 in the mesh image, measured by lane D) it does something else, which supports
    the same rule: asked to remove only the round end, it fills the WHOLE slot - side walls and square end too - and
    returns a plain 6-face block. Either way a slot end is not a feature that comes out on its own, so the
    recogniser source never proposes one. This pins both outcomes; a build where the end alone comes out (only its
    own faces gone) would fail here and send the rule back for review."""
    shape = slotted()
    [end] = [f for f in D.recognise(shape)[0] if f["kind"] == "slot_end"]
    faces, _ = D._faces(shape)
    before = D._face_count(shape)
    out, history, why = D.defeature(shape, [faces[i - 1] for i in end["faces"]])
    if out is None:   # the DEVLOG's outcome: a no-op, refused by the ported check
        assert why
        return
    removed = before - D._face_count(out)
    assert removed > len(end["faces"]), (
        f"OCCT removed only the slot end ({removed} faces): the 'never a slot end' rule needs review")


# ------------------------------------------------------------------ the op through the real pool

def test_the_recognised_set_is_removed_in_one_call_and_declared(pool: WorkerPool, tmp_path: Path) -> None:
    ws = _ws(pool, holed_and_filleted(), tmp_path, "hf")
    features = D.recognised_features(pool, ws)
    [(op, targets, params, _)] = D.defeature_rows(features)
    new, diff = REGISTRY[op].apply(pool, ws, targets, params, timeout_s=120)
    assert diff.params["via"] == "batch" and sorted(diff.params["removed_groups"]) == ["g0", "g1"]
    assert diff.params["not_removed"] == {}
    assert set(targets) <= set(diff.declared_removed)          # every feature face declared removed
    t0 = diff.params["t0"]                                     # his T0, reported
    assert t0["valid"] and t0["closed"] and t0["solids_before"] == t0["solids_after"] == 1 and t0["t0_pass"]
    assert t0["volume_change"] != 0.0
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape
    after = TopoDS_Shape()
    BRepTools.Read_s(after, new.brep_path, BRep_Builder())
    assert [f for f in D.recognise(after)[0] if f["kind"] in ("hole", "blend")] == []
    assert D._face_count(after) == 6                           # back to a plain block


def test_the_plain_op_removes_any_site_faces(pool: WorkerPool, tmp_path: Path) -> None:
    ws = _ws(pool, holed_and_filleted(), tmp_path, "plain")
    blend = next(f for f in D.recognised_features(pool, ws) if f["kind"] == "blend")
    new, diff = REGISTRY["defeature"].apply(pool, ws, blend["face_ids"], {"source": "site"}, timeout_s=120)
    assert set(blend["face_ids"]) <= set(diff.declared_removed) and diff.params["removed_groups"]


# ------------------------------------------------------------------ batch failure, and the faces-gone rule

def _sigs(shape: Any, faces: list[int]) -> tuple:
    allsigs = D.face_sigs(shape)
    return tuple(allsigs[i - 1][0] for i in faces)


def test_when_the_batch_fails_each_feature_is_removed_on_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    shape = holed_and_filleted()
    features, diag = D.recognise(shape)
    real = D.defeature

    def no_batch(s: Any, faces: Any) -> Any:
        if len(faces) > max(len(f["faces"]) for f in features):
            return None, None, "defeaturing failed"
        return real(s, faces)

    monkeypatch.setattr(D, "defeature", no_batch)
    out, history, removed, failed, via = D.remove_features(
        shape, [(f["id"], _sigs(shape, f["faces"])) for f in features], 1e-5 * diag)
    assert via == "one_at_a_time" and sorted(removed) == sorted(f["id"] for f in features) and failed == {}
    assert history is not None and D._face_count(out) == 6


def test_a_feature_counts_as_removed_only_when_its_faces_are_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    """The kernel 'succeeds' but one feature's faces are still there (as OCCT does for a slot end): not removed."""
    shape = holed_and_filleted()
    features, diag = D.recognise(shape)
    hole = next(f for f in features if f["kind"] == "hole")
    blend = next(f for f in features if f["kind"] == "blend")
    real = D.defeature
    faces, _ = D._faces(shape)

    def only_the_hole(s: Any, fs: Any) -> Any:
        return real(s, [faces[i - 1] for i in hole["faces"]])

    monkeypatch.setattr(D, "defeature", only_the_hole)
    _, _, removed, failed, via = D.remove_features(
        shape, [(hole["id"], _sigs(shape, hole["faces"])), (blend["id"], _sigs(shape, blend["faces"]))], 1e-5 * diag)
    assert via == "batch" and removed == [hole["id"]] and failed == {blend["id"]: "the kernel left it in place"}
