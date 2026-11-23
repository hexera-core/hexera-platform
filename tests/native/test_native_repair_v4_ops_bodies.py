# Responsibility: Prove on the REAL kernel that the v4 body ops share faces between touching bodies
#                 (Review Focus 1: a STEP round trip leaves coincident duplicate faces, which are NOT
#                 an overlap), cut a tiny overlap only within the fidelity bound, decline otherwise,
#                 keep untouched ids alive, and declare every removal.
# Boundaries: the native tier. The full verdict (Tasks 2-5) is not in this branch; the assertions
#             marked "VERDICT STAND-IN" measure OCCT properties directly and should be tightened to
#             the verdict when it lands.
from __future__ import annotations

from collections.abc import Iterator
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
from OCP.STEPCAFControl import STEPCAFControl_Reader, STEPCAFControl_Writer  # noqa: E402
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Reader, STEPControl_Writer  # noqa: E402
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


def _step_round_trip(shape: Any, path: Path) -> Any:
    writer = STEPControl_Writer()
    writer.Transfer(shape, STEPControl_AsIs)
    writer.Write(str(path))
    reader = STEPControl_Reader()
    reader.ReadFile(str(path))
    reader.TransferRoots()
    return reader.OneShape()


def _xcaf_round_trip(shape: Any, path: Path) -> Any:
    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    app.InitDocument(doc)
    XCAFDoc_DocumentTool.ShapeTool_s(doc.Main()).AddShape(shape, False)
    writer = STEPCAFControl_Writer()
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


def test_cut_by_beyond_the_bound_declines_without_clamping(pool: WorkerPool, tmp_path: Path) -> None:
    ws = _overlap_ws(pool, tmp_path, 5.0)
    keeper, other = _bodies(ws)
    siblings = sorted(p.name for p in tmp_path.iterdir())

    new, diff = Conformalise().apply(pool, ws, [keeper, other],
                                     {"mode": f"cut_by:{keeper}"}, timeout_s=TIMEOUT)

    assert diff.params["declined"] == "exceeds_fidelity"
    assert new is ws and new.brep_path == ws.brep_path
    assert diff.modified == {} and diff.removed == [] and diff.declared_removed == []
    assert sorted(p.name for p in tmp_path.iterdir()) == siblings  # nothing was written
    # A decline is not a clamp: the part on disk still overlaps by the full 5 mm.
    a, b = _solids(_read(ws.brep_path))
    assert _common_volume(a, b) == pytest.approx(100 * 100 * 5.0, rel=1e-6)


def test_fuzzy_above_the_bound_declines(pool: WorkerPool, tmp_path: Path) -> None:
    # a 10 mm part has bound min(0.1, 0.001 * 8.7) = 0.0087 mm, so 0.05 mm of fuzz is outside it
    small = Workspace.from_brep(pool, _write(
        _compound(_box(0, 0, 0, 5, 5, 5), _box(5, 0, 0, 5, 5, 5)), tmp_path / "small.brep"))
    _, diff = Conformalise().apply(pool, small, _bodies(small),
                                   {"mode": "fragment", "fuzzy_mm": 0.05}, timeout_s=TIMEOUT)
    assert diff.params["declined"] == "exceeds_fidelity"


def test_fragment_never_resolves_an_overlap_by_adding_bodies(pool: WorkerPool, tmp_path: Path) -> None:
    ws = _overlap_ws(pool, tmp_path, 0.05)
    new, diff = Conformalise().apply(pool, ws, _bodies(ws), {"mode": "fragment"}, timeout_s=TIMEOUT)
    assert diff.params["declined"] == "unresolved_overlap"
    assert new is ws


def test_fuse_is_not_an_op_and_not_a_mode(pool: WorkerPool, tmp_path: Path) -> None:
    assert [op.name for op in OPS] == ["conformalise", "remove_duplicate_faces"]
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
