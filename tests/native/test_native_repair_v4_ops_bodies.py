# Responsibility: Prove on the REAL kernel that the v4 body ops share faces between touching bodies
#                 (Review Focus 1: a STEP round trip leaves coincident duplicate faces, which are NOT
#                 an overlap), cut a tiny overlap only within the fidelity bound, decline otherwise,
#                 keep untouched ids alive, and declare every removal.
# Boundaries: the native tier. The full verdict (Tasks 2-5) is not in this branch; the assertions
#             marked "VERDICT STAND-IN" measure OCCT properties directly and should be tightened to
#             the verdict when it lands.
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("OCP.BRepAlgoAPI", reason="the CAD kernel is a native-tier dependency")

from OCP.Bnd import Bnd_Box  # noqa: E402
from OCP.BRep import BRep_Builder  # noqa: E402
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common  # noqa: E402
from OCP.BRepBndLib import BRepBndLib  # noqa: E402
from OCP.BRepCheck import BRepCheck_Analyzer  # noqa: E402
from OCP.BRepGProp import BRepGProp  # noqa: E402
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox  # noqa: E402
from OCP.BRepTools import BRepTools  # noqa: E402
from OCP.gp import gp_Pnt  # noqa: E402
from OCP.GProp import GProp_GProps  # noqa: E402
from OCP.Interface import Interface_Static  # noqa: E402
from OCP.STEPCAFControl import STEPCAFControl_Reader, STEPCAFControl_Writer  # noqa: E402
from OCP.STEPControl import (  # noqa: E402
    STEPControl_AsIs,
    STEPControl_Reader,
    STEPControl_ShellBasedSurfaceModel,
    STEPControl_Writer,
)
from OCP.TCollection import TCollection_ExtendedString  # noqa: E402
from OCP.TDF import TDF_LabelSequence  # noqa: E402
from OCP.TDocStd import TDocStd_Document  # noqa: E402
from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID  # noqa: E402
from OCP.TopExp import TopExp  # noqa: E402
from OCP.TopoDS import TopoDS_Compound, TopoDS_Shape  # noqa: E402
from OCP.TopTools import (  # noqa: E402
    TopTools_IndexedDataMapOfShapeListOfShape,
    TopTools_IndexedMapOfShape,
)
from OCP.XCAFApp import XCAFApp_Application  # noqa: E402
from OCP.XCAFDoc import XCAFDoc_DocumentTool  # noqa: E402

from meshpipeline.cad.repair_v4.ops.bodies import OPS, Conformalise, RemoveDuplicateFaces  # noqa: E402
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool  # noqa: E402
from meshpipeline.cad.repair_v4.workspace import UnknownTarget, Workspace  # noqa: E402

TIMEOUT = 300.0


@pytest.fixture
def pool() -> Iterator[WorkerPool]:
    p = WorkerPool(1)
    try:
        yield p
    finally:
        p.close()


# ---- OCCT helpers (the tests measure the kernel; nothing here is mocked) -----------------------

def _box(x: float, y: float, z: float, dx: float, dy: float, dz: float) -> Any:
    return BRepPrimAPI_MakeBox(gp_Pnt(x, y, z), dx, dy, dz).Shape()


def _compound(*shapes: Any) -> Any:
    comp = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(comp)
    for s in shapes:
        builder.Add(comp, s)
    return comp


def _write(shape: Any, path: Path) -> str:
    assert BRepTools.Write_s(shape, str(path))
    return str(path)


def _read(path: str) -> Any:
    shape = TopoDS_Shape()
    assert BRepTools.Read_s(shape, path, BRep_Builder())
    return shape


def _get_static(name: str) -> Any:
    return Interface_Static.IVal_s(name) if Interface_Static.IsSet_s(name) and \
        Interface_Static.CVal_s(name).lstrip("-").isdigit() else Interface_Static.CVal_s(name)


@contextmanager
def _statics(statics: dict[str, Any]) -> Iterator[None]:
    """Set Interface_Static switches for the duration of a write and RESTORE them afterwards, so no
    value leaks into other tests (they are process-wide). They exist only once a reader/writer has
    been constructed - a set before that silently returns False - so callers construct first, and
    every return value is asserted here."""
    previous = {name: _get_static(name) for name in statics}
    try:
        for name, value in statics.items():
            ok = (Interface_Static.SetIVal_s(name, value) if isinstance(value, int)
                  else Interface_Static.SetCVal_s(name, value))
            assert ok, f"Interface_Static {name}={value!r} was not accepted"
        yield
    finally:
        for name, value in previous.items():
            if isinstance(value, int):
                Interface_Static.SetIVal_s(name, value)
            else:
                Interface_Static.SetCVal_s(name, value)


def _adv_faces(path: Path) -> int:
    """ADVANCED_FACE entities in the written STEP FILE: what the writer emitted, before any reader."""
    return int(path.read_text().count("ADVANCED_FACE("))


def _step_round_trip(shape: Any, path: Path, statics: dict[str, Any] | None = None,
                     mode: Any = STEPControl_AsIs) -> Any:
    writer = STEPControl_Writer()
    with _statics(statics or {}):
        writer.Transfer(shape, mode)
        writer.Write(str(path))
    reader = STEPControl_Reader()
    reader.ReadFile(str(path))
    reader.TransferRoots()
    return reader.OneShape()


def _xcaf_round_trip(shape: Any, path: Path, statics: dict[str, Any] | None = None,
                     make_assembly: bool = False) -> Any:
    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    app.InitDocument(doc)
    XCAFDoc_DocumentTool.ShapeTool_s(doc.Main()).AddShape(shape, make_assembly)
    writer = STEPCAFControl_Writer()
    with _statics(statics or {}):
        writer.Transfer(doc)
        writer.Write(str(path))
    back = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    app.InitDocument(back)
    reader = STEPCAFControl_Reader()
    reader.ReadFile(str(path))
    reader.Transfer(back)
    tool = XCAFDoc_DocumentTool.ShapeTool_s(back.Main())
    labels = TDF_LabelSequence()
    tool.GetFreeShapes(labels)
    return _compound(*[tool.GetShape_s(labels.Value(i)) for i in range(1, labels.Length() + 1)])


def _count(shape: Any, kind: Any) -> int:
    indexed = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, kind, indexed)
    return int(indexed.Extent())


def _solids(shape: Any) -> list[Any]:
    indexed = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_SOLID, indexed)
    return [indexed.FindKey(i) for i in range(1, indexed.Extent() + 1)]


def _volume(shape: Any) -> float:
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return float(props.Mass())


def _shared_faces(shape: Any) -> int:
    """Faces that belong to two solids at once (one TShape), i.e. a conformal interface."""
    owners = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_FACE, TopAbs_SOLID, owners)
    return sum(1 for i in range(1, owners.Extent() + 1) if owners.FindFromIndex(i).Size() == 2)


def _common_volume(a: Any, b: Any) -> float:
    common = BRepAlgoAPI_Common(a, b).Shape()
    return _volume(common) if _count(common, TopAbs_SOLID) else 0.0


def _bbox(shape: Any) -> tuple[float, ...]:
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    return tuple(box.Get())


def _face_indices_on_plane_x(shape: Any, x: float) -> list[int]:
    faces = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, faces)
    found = []
    for i in range(1, faces.Extent() + 1):
        x0, _, _, x1, _, _ = _bbox(faces.FindKey(i))
        if abs(x0 - x) < 1e-6 and abs(x1 - x) < 1e-6:
            found.append(i)
    return found


def _id_of_face(ws: Workspace, index: int) -> str:
    (sid,) = [s for s, n in ws.indices.items() if n == index and s.startswith("F")]
    return sid


def _touching_after_step(tmp_path: Path) -> tuple[Any, str]:
    """Two 10 mm boxes sharing the x=10 face, after a STEP round trip: the shape and its BREP."""
    shape = _step_round_trip(_compound(_box(0, 0, 0, 10, 10, 10), _box(10, 0, 0, 10, 10, 10)),
                             tmp_path / "touch.step")
    return shape, _write(shape, tmp_path / "touch.brep")


def _bodies(ws: Workspace) -> list[str]:
    return sorted((s for s in ws.ids if s.startswith("B")), key=lambda s: ws.indices[s])


# ---- conformalise: touching bodies (Review Focus 1) --------------------------------------------

def test_step_round_trip_leaves_duplicate_faces_that_are_not_an_overlap(tmp_path: Path) -> None:
    """The premise of Review Focus 1, measured: after the round trip the contact is two coincident
    faces (12 faces, none shared) while the bodies' Common volume is zero - touching, not overlapping."""
    shape, _ = _touching_after_step(tmp_path)
    a, b = _solids(shape)
    assert _count(shape, TopAbs_FACE) == 12
    assert _shared_faces(shape) == 0
    assert _common_volume(a, b) < 1e-6


def test_conformalise_fragment_shares_the_contact_face(pool: WorkerPool, tmp_path: Path) -> None:
    shape, path = _touching_after_step(tmp_path)
    ws = Workspace.from_brep(pool, path)
    bodies = _bodies(ws)
    assert len(bodies) == 2

    new, diff = Conformalise().apply(pool, ws, bodies, {"mode": "fragment"}, timeout_s=TIMEOUT)

    out = _read(new.brep_path)
    # VERDICT STAND-IN: shared interface, body count, no overlap, valid B-rep, volume conserved.
    assert out.ShapeType().name.endswith("COMPOUND")
    assert _count(out, TopAbs_SOLID) == 2
    assert _count(out, TopAbs_FACE) == 11
    assert _shared_faces(out) == 1
    a, b = _solids(out)
    assert _common_volume(a, b) < 1e-6
    assert BRepCheck_Analyzer(out).IsValid()
    assert _volume(out) == pytest.approx(2000.0)
    # Identity: both bodies keep their ids (1:1 Modified), nothing is left unnamed.
    assert _bodies(new) == bodies
    assert "unattributed" not in diff.params
    # The collapsed duplicate is declared: one face and the edges that merged with it.
    assert len([d for d in diff.declared_removed if d.startswith("F")]) == 1
    assert set(diff.declared_removed) <= set(diff.removed)
    assert all(r in diff.declared_removed for r in diff.removed)
    assert diff.declared_added == []


def test_step_does_not_keep_a_shared_face_so_conformalise_must_be_reapplied(
    pool: WorkerPool, tmp_path: Path,
) -> None:
    """OCCT's STEP writer emits each solid's shell separately, so a face shared in memory comes back
    as two faces after BOTH the plain and the XCAF round trip (measured here, not assumed). Sharing
    therefore has to be re-established AFTER the last STEP read: conformalise is idempotent on the
    re-read part and restores the single shared face."""
    _, path = _touching_after_step(tmp_path)
    ws = Workspace.from_brep(pool, path)
    new, _ = Conformalise().apply(pool, ws, _bodies(ws), {"mode": "fragment"}, timeout_s=TIMEOUT)
    conformal = _read(new.brep_path)
    assert _shared_faces(conformal) == 1

    plain = _step_round_trip(conformal, tmp_path / "plain.step")
    xcaf = _xcaf_round_trip(conformal, tmp_path / "xcaf.step")
    assert _shared_faces(plain) == 0 and _count(plain, TopAbs_FACE) == 12
    assert _shared_faces(xcaf) == 0 and _count(xcaf, TopAbs_FACE) == 12

    # The loss is the WRITER's: the file itself holds 12 ADVANCED_FACE entities for a shape with
    # 11 distinct faces, so no reader setting could matter. Every cell sets its statics AFTER
    # constructing the writer and asserts they were accepted.
    assert _count(conformal, TopAbs_FACE) == 11
    assert _adv_faces(tmp_path / "plain.step") == 12 and _adv_faces(tmp_path / "xcaf.step") == 12
    for asm in (0, 1, 2):
        for schema in ("AP214CD", "AP242DIS"):
            for surface_curve in (0, 1):
                st = {"write.step.assembly": asm, "write.step.schema": schema,
                      "write.surfacecurve.mode": surface_curve}
                plain_path, xcaf_path = tmp_path / "s.step", tmp_path / "x.step"
                for rt, written in ((_step_round_trip(conformal, plain_path, st), plain_path),
                                    (_xcaf_round_trip(conformal, xcaf_path, st), xcaf_path)):
                    assert _adv_faces(written) == 12, (st, written.name)
                    assert _shared_faces(rt) == 0 and _count(rt, TopAbs_FACE) == 12, st
    asm_path = tmp_path / "asm.step"
    asm_shape = _xcaf_round_trip(conformal, asm_path, {"write.step.assembly": 0}, make_assembly=True)
    assert _adv_faces(asm_path) == 12 and _shared_faces(asm_shape) == 0
    sb_path = tmp_path / "sb.step"
    sb_shape = _step_round_trip(conformal, sb_path, mode=STEPControl_ShellBasedSurfaceModel)
    assert _adv_faces(sb_path) == 12 and _shared_faces(sb_shape) == 0
    # and the switches did not leak out of the context: a plain write is back to the defaults
    assert _adv_faces(tmp_path / "plain.step") == 12

    again = Workspace.from_brep(pool, _write(xcaf, tmp_path / "xcaf.brep"))
    final, _ = Conformalise().apply(pool, again, _bodies(again), {"mode": "fragment"}, timeout_s=TIMEOUT)
    out = _read(final.brep_path)
    assert _shared_faces(out) == 1 and _count(out, TopAbs_SOLID) == 2
    assert BRepCheck_Analyzer(out).IsValid()


def test_untouched_body_keeps_every_id(pool: WorkerPool, tmp_path: Path) -> None:
    """The IsSame contract through a boolean: a third body that takes no part is rebuilt as the same
    TShape, so none of its ids is retired or re-minted."""
    shape = _step_round_trip(
        _compound(_box(0, 0, 0, 10, 10, 10), _box(10, 0, 0, 10, 10, 10), _box(50, 50, 50, 5, 5, 5)),
        tmp_path / "three.step")
    ws = Workspace.from_brep(pool, _write(shape, tmp_path / "three.brep"))
    bodies = _bodies(ws)
    far = next(s for s in bodies if _bbox(_solids(_read(ws.brep_path))[ws.indices[s] - 1])[0] > 40)
    pair = [s for s in bodies if s != far]
    far_ids = {sid: ws.ids[sid] for sid in ws.ids if sid not in pair}

    new, diff = Conformalise().apply(pool, ws, pair, {"mode": "fragment"}, timeout_s=TIMEOUT)

    # every entity of the far body (and its own faces/edges) survives with the same id and name
    far_faces = [s for s, n in ws.indices.items() if s.startswith("F") and n in
                 _far_face_indices(_read(ws.brep_path))]
    assert far_faces and all(f in new.ids and new.ids[f] == ws.ids[f] for f in far_faces)
    assert far in new.ids and new.ids[far] == far_ids[far]
    assert "unattributed" not in diff.params
    assert _count(_read(new.brep_path), TopAbs_SOLID) == 3


def _far_face_indices(shape: Any) -> set[int]:
    faces = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, faces)
    return {i for i in range(1, faces.Extent() + 1) if _bbox(faces.FindKey(i))[0] > 40}


# ---- conformalise: cut_by and the fidelity bound ----------------------------------------------

def _overlap_ws(pool: WorkerPool, tmp_path: Path, depth: float) -> Workspace:
    shape = _compound(_box(0, 0, 0, 100, 100, 100), _box(100 - depth, 0, 0, 100, 100, 100))
    return Workspace.from_brep(pool, _write(shape, tmp_path / "overlap.brep"))


def test_cut_by_within_the_bound_is_valid_and_faithful(pool: WorkerPool, tmp_path: Path) -> None:
    ws = _overlap_ws(pool, tmp_path, 0.05)
    keeper, other = _bodies(ws)
    before = _read(ws.brep_path)

    new, diff = Conformalise().apply(pool, ws, [keeper, other],
                                     {"mode": f"cut_by:{keeper}"}, timeout_s=TIMEOUT)

    out = _read(new.brep_path)
    assert "declined" not in diff.params
    # VERDICT STAND-IN: valid, same body count, no overlap left, interface shared, and faithful:
    # the keeper is untouched, the other body lost exactly the overlap, the envelope is unchanged.
    assert BRepCheck_Analyzer(out).IsValid()
    assert _count(out, TopAbs_SOLID) == 2
    a, b = _solids(out)
    assert _common_volume(a, b) < 1e-6
    assert _shared_faces(out) == 1
    assert _volume(a) == pytest.approx(1_000_000.0)
    assert _volume(b) == pytest.approx(1_000_000.0 - 100 * 100 * 0.05, rel=1e-6)
    assert _bbox(out) == pytest.approx(_bbox(before))
    assert _bodies(new) == [keeper, other]
    # the cut's own removals are declared: nothing leaves the workspace unexplained
    assert diff.removed and set(diff.removed) == set(diff.declared_removed)


def test_cut_by_beyond_the_old_bound_cuts_and_reports_the_depth(pool: WorkerPool, tmp_path: Path) -> None:
    """Lane C7, the owner's 2026-11-24 goal: the fidelity bound no longer gates an edit. A 5 mm overlap (50x the old
    bound) is cut; its measured depth is REPORTED in the Diff instead of refusing the op."""
    ws = _overlap_ws(pool, tmp_path, 5.0)
    keeper, other = _bodies(ws)
    new, diff = Conformalise().apply(pool, ws, [keeper, other],
                                     {"mode": f"cut_by:{keeper}"}, timeout_s=TIMEOUT)
    assert "declined" not in diff.params, diff.params
    (overlap,) = diff.params["measured"]["overlaps"]
    assert overlap["depth_mm"] == pytest.approx(5.0, rel=1e-3) and overlap["depth_mm"] > overlap["bound_mm"]
    a, b = _solids(_read(new.brep_path))
    assert _common_volume(a, b) < 1e-6


def test_fuzzy_above_the_old_bound_is_accepted(pool: WorkerPool, tmp_path: Path) -> None:
    # a 10 mm part has bound min(0.1, 0.001 * 8.7) = 0.0087 mm; 0.05 mm of fuzz was refused before B12
    small = Workspace.from_brep(pool, _write(
        _compound(_box(0, 0, 0, 5, 5, 5), _box(5, 0, 0, 5, 5, 5)), tmp_path / "small.brep"))
    _, diff = Conformalise().apply(pool, small, _bodies(small),
                                   {"mode": "fragment", "fuzzy_mm": 0.05}, timeout_s=TIMEOUT)
    assert diff.params.get("declined") != "exceeds_fidelity", diff.params


def test_fragment_never_resolves_an_overlap_by_adding_bodies(pool: WorkerPool, tmp_path: Path) -> None:
    ws = _overlap_ws(pool, tmp_path, 0.05)
    new, diff = Conformalise().apply(pool, ws, _bodies(ws), {"mode": "fragment"}, timeout_s=TIMEOUT)
    assert diff.params["declined"] == "unresolved_overlap"
    assert new is ws


def test_fuse_is_its_own_op_never_a_conformalise_mode(pool: WorkerPool, tmp_path: Path) -> None:
    """Lane C7: the no-fuse rule is gone, and fuse is an op of its own (tests/native/test_native_repair_v4_c7_body_ops.py),
    not a hidden mode of conformalise."""
    # rescale (v5 step 5, units) and tweak (v5 step 6, misalignment) joined the body ops in the CRS v5 import (#540)
    assert [op.name for op in OPS] == ["conformalise", "remove_duplicate_faces", "fuse", "trim_unbounded",
                                       "rescale", "tweak"]
    assert "fuse" not in Conformalise.params_schema["properties"]["mode"]["pattern"]
    ws = _overlap_ws(pool, tmp_path, 0.05)
    with pytest.raises(ValueError):
        Conformalise().apply(pool, ws, _bodies(ws), {"mode": "fuse"}, timeout_s=TIMEOUT)


# ---- remove_duplicate_faces --------------------------------------------------------------------

def test_duplicate_face_is_removed_and_declared(pool: WorkerPool, tmp_path: Path) -> None:
    shape, path = _touching_after_step(tmp_path)
    ws = Workspace.from_brep(pool, path)
    dup_a, dup_b = (_id_of_face(ws, i) for i in _face_indices_on_plane_x(shape, 10.0))
    bodies = _bodies(ws)

    new, diff = RemoveDuplicateFaces().apply(
        pool, ws, [dup_a, dup_b], {"pairs": [[dup_a, dup_b]]}, timeout_s=TIMEOUT)

    out = _read(new.brep_path)
    assert "declined" not in diff.params
    assert _count(out, TopAbs_FACE) == 11 and _shared_faces(out) == 1  # VERDICT STAND-IN
    assert _count(out, TopAbs_SOLID) == 2 and BRepCheck_Analyzer(out).IsValid()
    # exactly one of the pair is gone, the other kept its id, and the removal is DECLARED
    gone = [f for f in (dup_a, dup_b) if f not in new.ids]
    assert gone == [dup_b]  # lowest index survives
    assert dup_a in new.ids
    assert dup_b in diff.declared_removed and dup_b in diff.removed
    assert set(diff.removed) == set(diff.declared_removed)  # nothing removed that was not declared
    with pytest.raises(UnknownTarget):
        new.resolve([dup_b])
    assert _bodies(new) == bodies


def test_non_coincident_faces_are_declined(pool: WorkerPool, tmp_path: Path) -> None:
    shape, path = _touching_after_step(tmp_path)
    ws = Workspace.from_brep(pool, path)
    dup_a = _id_of_face(ws, _face_indices_on_plane_x(shape, 10.0)[0])
    outer = _id_of_face(ws, _face_indices_on_plane_x(shape, 0.0)[0])
    new, diff = RemoveDuplicateFaces().apply(
        pool, ws, [dup_a, outer], {"pairs": [[dup_a, outer]]}, timeout_s=TIMEOUT)
    assert diff.params["declined"] == "not_coincident"
    assert new is ws and diff.declared_removed == []


def test_conformalise_that_would_share_nothing_declines(pool: WorkerPool, tmp_path: Path) -> None:
    """Boxes 1e-4 mm apart at fuzzy 0 (STEP rounding): fragmenting leaves them separate. That must
    not be reported as success - the search would record progress that did not happen."""
    ws = Workspace.from_brep(pool, _write(
        _compound(_box(0, 0, 0, 10, 10, 10), _box(10.0001, 0, 0, 10, 10, 10)), tmp_path / "gap.brep"))
    new, diff = Conformalise().apply(pool, ws, _bodies(ws), {"mode": "fragment"}, timeout_s=TIMEOUT)
    # the fragment changed nothing at all, which lane C10's `no_change` says first and more precisely than the
    # shared-interface check behind it; either way it is a decline, never a success
    assert diff.params["declined"] in ("no_change", "no_shared_interface")
    assert new is ws and diff.modified == {} and diff.declared_removed == []


def test_fuzzy_within_the_bound_closes_a_rounding_gap(pool: WorkerPool, tmp_path: Path) -> None:
    """The same 1e-4 mm gap with fuzzy 1e-3 mm (bound for this part is 0.0087 mm) is shared."""
    ws = Workspace.from_brep(pool, _write(
        _compound(_box(0, 0, 0, 10, 10, 10), _box(10.0001, 0, 0, 10, 10, 10)), tmp_path / "gap.brep"))
    new, diff = Conformalise().apply(pool, ws, _bodies(ws),
                                     {"mode": "fragment", "fuzzy_mm": 1e-3}, timeout_s=TIMEOUT)
    assert "declined" not in diff.params
    out = _read(new.brep_path)
    assert _shared_faces(out) == 1 and _count(out, TopAbs_SOLID) == 2
    assert BRepCheck_Analyzer(out).IsValid()


def test_thin_but_vast_cut_is_cut_and_its_volume_change_reported(pool: WorkerPool, tmp_path: Path) -> None:
    """It removes 5% of a 1 mm plate: over the old 2% cap, which no longer gates (lane C7); the share is reported."""
    shape = _compound(_box(0, 0, 0, 100, 100, 100), _box(99.95, 0, 0, 1, 100, 100))
    ws = Workspace.from_brep(pool, _write(shape, tmp_path / "plate.brep"))
    keeper, plate = _bodies(ws)
    new, diff = Conformalise().apply(pool, ws, [keeper, plate],
                                     {"mode": f"cut_by:{keeper}"}, timeout_s=TIMEOUT)
    assert "declined" not in diff.params, diff.params
    (overlap,) = diff.params["measured"]["overlaps"]
    assert overlap["volume_fraction"] == pytest.approx(0.05, rel=1e-3)


# ---- the overlap DEPTH is how far a cut moves the victim's surface (lane B6/B7) -----------------


def _rotated(shape: Any, *, about_z_deg: float, about_y_deg: float) -> Any:
    import math

    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Ax1, gp_Dir, gp_Trsf

    t1, t2 = gp_Trsf(), gp_Trsf()
    t1.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), math.radians(about_z_deg))
    t2.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 1, 0)), math.radians(about_y_deg))
    t2.Multiply(t1)
    return BRepBuilderAPI_Transform(shape, t2, True).Shape()


def _inclined_slab_ws(pool: WorkerPool, tmp_path: Path, thickness: float) -> Workspace:
    """Two 10 mm cubes overlapping in a `thickness` slab, the whole part turned 30 deg about z and 20 about y, so
    the slab is INCLINED: its axis-aligned bounding box is millimetres thick though the slab is not."""
    shape = _compound(_box(0, 0, 0, 10, 10, 10), _box(10 - thickness, 0, 0, 10, 10, 10))
    return Workspace.from_brep(pool, _write(_rotated(shape, about_z_deg=30, about_y_deg=20), tmp_path / "incl.brep"))


def test_a_thin_inclined_overlap_under_the_bound_is_cut_not_declined(pool: WorkerPool, tmp_path: Path) -> None:
    """The turned part's bound is min(0.1, 0.001 x its diagonal) = 0.035 mm; a 0.02 mm slab is UNDER it. The old
    depth, max(2V/A, thinnest axis-aligned extent of the overlap), read 8.13 mm here and declined it."""
    from meshpipeline.cad.repair_v4.body_kernel import _cut_depth, fidelity_bound
    from meshpipeline.cad.repair_v4.ws_kernel import diagonal_of

    ws = _inclined_slab_ws(pool, tmp_path, 0.02)
    shape = _read(ws.brep_path)
    a, b = _solids(shape)
    bound = fidelity_bound(diagonal_of(shape))
    _, depth, method = _cut_depth(b, a, 0.0)
    assert method == "measured" and depth == pytest.approx(0.02, rel=0.05) and depth < bound
    box = _bbox(BRepAlgoAPI_Common(a, b).Shape())
    assert min(box[3] - box[0], box[4] - box[1], box[5] - box[2]) > 10 * bound      # what the old measure read

    keeper, other = _bodies(ws)
    new, diff = Conformalise().apply(pool, ws, [keeper, other], {"mode": f"cut_by:{keeper}"}, timeout_s=TIMEOUT)
    assert "declined" not in diff.params, diff.params
    out = _read(new.brep_path)
    assert BRepCheck_Analyzer(out).IsValid() and _count(out, TopAbs_SOLID) == 2
    s1, s2 = _solids(out)
    assert _common_volume(s1, s2) < 1e-6 and _shared_faces(out) >= 1


def test_a_thick_inclined_overlap_is_cut_and_reports_its_real_depth(pool: WorkerPool, tmp_path: Path) -> None:
    ws = _inclined_slab_ws(pool, tmp_path, 0.5)
    keeper, other = _bodies(ws)
    _, diff = Conformalise().apply(pool, ws, [keeper, other], {"mode": f"cut_by:{keeper}"}, timeout_s=TIMEOUT)
    assert "declined" not in diff.params, diff.params
    (overlap,) = diff.params["measured"]["overlaps"]
    assert overlap["method"] == "measured" and overlap["depth_mm"] == pytest.approx(0.5, rel=0.05)


def test_the_depth_is_the_surface_movement_for_a_slab_and_a_corner_cube() -> None:
    """A slab of thickness t: the victim's face moves t. A corner cube of side s: the victim's corner moves s - where
    the mean thickness 2V/A would read s/3."""
    from meshpipeline.cad.repair_v4.body_kernel import _cut_depth

    keeper = _box(0, 0, 0, 10, 10, 10)
    _, slab, m1 = _cut_depth(_box(9.7, 0, 0, 10, 10, 10), keeper, 0.0)
    _, cube, m2 = _cut_depth(_box(9.7, 9.7, 9.7, 10, 10, 10), keeper, 0.0)
    assert (m1, m2) == ("measured", "measured")
    assert slab == pytest.approx(0.3, rel=0.02)
    assert cube == pytest.approx(0.3, rel=0.05)                                     # not 0.1 (= 2V/A)


def test_a_duplicate_or_contained_body_falls_back_to_the_mean_thickness() -> None:
    """Two coincident copies: the overlap is the whole body and no victim surface lies inside the keeper alone, so
    there is no movement to measure. The depth is then the mean thickness 2V/A of the overlap, never 0."""
    from meshpipeline.cad.repair_v4.body_kernel import _cut_depth

    _, depth, method = _cut_depth(_box(0, 0, 0, 10, 10, 10), _box(0, 0, 0, 10, 10, 10), 0.0)
    assert method == "mean_thickness" and depth == pytest.approx(10 / 3, rel=1e-6)
