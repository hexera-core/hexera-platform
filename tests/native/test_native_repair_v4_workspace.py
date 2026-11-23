# Responsibility: Prove on the REAL kernel that a BRepTools_History record becomes stable ids:
#                 splitting one box face maps its id to two generated ids, names propagate,
#                 untouched faces keep their ids, and the replaced id is an UnknownTarget.
# Boundaries: the native tier; hand-built history records and fork/resolve rules are unit-tested.
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("OCP.BRepAlgoAPI", reason="the CAD kernel is a native-tier dependency")

from meshpipeline.cad.repair_v4.worker_pool import WorkerPool  # noqa: E402
from meshpipeline.cad.repair_v4.workspace import UnknownTarget, Workspace, apply_history  # noqa: E402


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch) -> Iterator[WorkerPool]:
    monkeypatch.setenv("HEXERA_TEST_KERNEL_FNS", "1")
    p = WorkerPool(1)
    try:
        yield p
    finally:
        p.close()


def _box_brep(tmp_path: Path) -> str:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.BRepTools import BRepTools

    path = tmp_path / "box.brep"
    assert BRepTools.Write_s(BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape(), str(path))
    return str(path)


def _face_id_at_z(ws: Workspace, pool: WorkerPool, z: float) -> str:
    """The stable id of the box face lying in the plane z=`z` (asked of the kernel, not guessed)."""
    result = pool.call("ws_face_ids_at_z", {"brep_path": ws.brep_path, "z": z,
                                            "quantum": ws.quantum}, timeout_s=60)
    assert result.ok, result.error
    assert result.value is not None
    (index,) = result.value["indices"]
    (face_id,) = [i for i, n in ws.indices.items() if n == index and i.startswith("F")]
    return face_id


def test_split_face_maps_old_id_to_two_generated_ids_and_names_propagate(
    pool: WorkerPool, tmp_path: Path,
) -> None:
    ws = Workspace.from_brep(pool, _box_brep(tmp_path))
    faces = [i for i in ws.ids if i.startswith("F")]
    assert len(faces) == 6
    for i in faces:
        ws.labels[i] = {"name": f"name-of-{i}"}
    top = _face_id_at_z(ws, pool, 10.0)

    result = pool.call("ws_split_face_by_edge", {
        "brep_path": ws.brep_path, "out_path": str(tmp_path / "split.brep"),
        "quantum": ws.quantum, "z": 10.0, "p1": [5.0, 0.0, 10.0], "p2": [5.0, 10.0, 10.0],
    }, timeout_s=60)
    assert result.ok, result.error
    assert result.value is not None
    after = apply_history(ws, result.value)

    diff = after.history[-1]
    assert diff.modified[top] and len(diff.modified[top]) == 2, diff
    kids = diff.modified[top]
    assert all(k.startswith("F") and k in after.ids for k in kids)
    assert top not in after.ids
    assert all(after.labels[k] == {"name": f"name-of-{top}"} for k in kids)
    # six faces before, one replaced by two: seven now, and the five untouched kept their ids
    assert len([i for i in after.ids if i.startswith("F")]) == 7
    untouched = [i for i in faces if i != top]
    assert all(i in after.ids and after.ids[i] == ws.ids[i] for i in untouched)
    assert all(after.labels[i] == {"name": f"name-of-{i}"} for i in untouched)
    # the parent is not mutated and the old id is now an addressable, explained failure
    assert top in ws.ids and ws.brep_path != after.brep_path
    with pytest.raises(UnknownTarget) as info:
        after.resolve([top])
    assert all(k in info.value.reason for k in kids)
    assert after.resolve(kids) == kids


def test_one_to_one_modified_face_keeps_its_stable_id(pool: WorkerPool, tmp_path: Path) -> None:
    """Cut a notch from one side: the top face is trimmed IN PLACE (bbox changes), so "face 41"
    must still be face 41 afterwards, with its label and a refreshed fingerprint."""
    ws = Workspace.from_brep(pool, _box_brep(tmp_path))
    top = _face_id_at_z(ws, pool, 10.0)
    ws.labels[top] = {"name": "the-top"}
    before = ws.ids[top]

    result = pool.call("ws_cut_box", {
        "brep_path": ws.brep_path, "out_path": str(tmp_path / "cut.brep"),
        "quantum": ws.quantum, "corner": [-1.0, -1.0, 5.0], "size": [4.0, 12.0, 6.0],
    }, timeout_s=60)
    assert result.ok, result.error
    assert result.value is not None
    after = apply_history(ws, result.value)

    assert after.history[-1].modified[top] == [top]
    assert top in after.ids and after.resolve([top]) == [top]
    assert after.ids[top] != before  # it really was trimmed: its bounding box moved
    assert after.labels[top] == {"name": "the-top"}
    assert _face_id_at_z(after, pool, 10.0) == top  # the kernel agrees it is the z=10 face


def test_every_index_survives_the_brep_round_trip(pool: WorkerPool, tmp_path: Path) -> None:
    """Identity rests on the written BREP reading back with the same entity at EVERY index: check
    all faces, edges and solids of the split result, not one face."""
    ws = Workspace.from_brep(pool, _box_brep(tmp_path))
    out = str(tmp_path / "split.brep")
    result = pool.call("ws_split_face_by_edge", {
        "brep_path": ws.brep_path, "out_path": out, "quantum": ws.quantum, "z": 10.0,
        "p1": [5.0, 0.0, 10.0], "p2": [5.0, 10.0, 10.0]}, timeout_s=60)
    assert result.ok, result.error
    assert result.value is not None
    reread = pool.call("ws_index", {"brep_path": out, "quantum": ws.quantum}, timeout_s=60)
    assert reread.ok and reread.value is not None
    assert reread.value["entities"] == result.value["entities"]
    assert len(result.value["entities"]) > 20  # 7 faces + edges + solid, all compared


def test_round_trip_guard_refuses_a_file_that_is_not_the_recorded_shape(
    pool: WorkerPool, tmp_path: Path,
) -> None:
    ws = Workspace.from_brep(pool, _box_brep(tmp_path))
    result = pool.call("ws_record_for_wrong_file", {
        "brep_path": ws.brep_path, "out_path": str(tmp_path / "wrong.brep"),
        "quantum": ws.quantum}, timeout_s=60)
    assert not result.ok
    assert result.error is not None and "IndexInstabilityError" in result.error


def test_record_for_a_stale_workspace_is_refused(pool: WorkerPool, tmp_path: Path) -> None:
    """A real record, applied to a workspace that has already moved on, fails loudly."""
    ws = Workspace.from_brep(pool, _box_brep(tmp_path))
    result = pool.call("ws_split_face_by_edge", {
        "brep_path": ws.brep_path, "out_path": str(tmp_path / "split.brep"),
        "quantum": ws.quantum, "z": 10.0, "p1": [5.0, 0.0, 10.0], "p2": [5.0, 10.0, 10.0]},
        timeout_s=60)
    assert result.ok and result.value is not None
    after = apply_history(ws, result.value)
    with pytest.raises(ValueError, match="stale workspace"):
        apply_history(after, result.value)
