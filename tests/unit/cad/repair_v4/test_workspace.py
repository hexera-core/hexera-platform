# Responsibility: Verify the v4 Workspace's identity rules without a kernel: fork independence,
#                 UnknownTarget on removed/replaced/unknown ids, monotonic ids, label inheritance,
#                 1:1 Modified keeping its id, and index-keyed binding under fingerprint collisions.
# Boundaries: hermetic - history records are hand-built; the real BRepTools_History mapping is
#             proven natively in tests/native/test_native_repair_v4_workspace.py.
from __future__ import annotations

import pytest

from meshpipeline.cad.repair_v4.types import Site
from meshpipeline.cad.repair_v4.workspace import UnknownTarget, Workspace, apply_history


def _ws() -> Workspace:
    return Workspace(
        brep_path="/tmp/a.brep",
        ids={"F1": "face:aa", "F2": "face:bb", "E1": "edge:cc", "B1": "solid:dd"},
        labels={"F1": {"name": "top"}, "F2": {"name": "side"}},
        history=[],
        original="/tmp/a.brep",
        indices={"F1": 1, "F2": 2, "E1": 1, "B1": 1},
    )


def _ent(kind: str, index: int, fp: str) -> dict:
    return {"type": kind, "index": index, "fp": fp}


_FP = {("face", 1): "face:aa", ("face", 2): "face:bb", ("edge", 1): "edge:cc",
       ("solid", 1): "solid:dd"}


def _row(kind: str, index: int, *, fp: str | None = None, now: int | None = None,
         removed: bool = False, modified: list | None = None,
         generated: list | None = None) -> dict:
    return {"type": kind, "index": index, "fp": fp or _FP[(kind, index)], "now": index if now is None else now,
            "removed": removed, "modified": modified or [], "generated": generated or []}


def _kid(kind: str, index: int) -> dict:
    return {"type": kind, "index": index}


def _record(op: str, entities: list, history: list, path: str = "/tmp/b.brep",
            old_path: str = "/tmp/a.brep", **extra) -> dict:
    return {"version": 1, "op": op, "params": {}, "old_path": old_path, "brep_path": path, "seconds": 0.0,
            "declared_added": [], "declared_removed": [], "entities": entities,
            "history": history, **extra}


def _split_f1() -> dict:
    """F1 (face 1) split in two by an op (new faces 1 and 3); everything else untouched."""
    rec = _record(
        "split",
        [_ent("face", 1, "face:a1"), _ent("face", 2, "face:bb"), _ent("face", 3, "face:a2"),
         _ent("edge", 1, "edge:cc"), _ent("edge", 2, "edge:new"), _ent("solid", 1, "solid:dd")],
        [_row("face", 1, now=0, modified=[_kid("face", 1), _kid("face", 3)]),
         _row("face", 2), _row("edge", 1), _row("solid", 1)])
    rec["params"] = {"at": 0.5}
    rec["seconds"] = 0.25
    return rec


def test_fork_is_independent() -> None:
    parent = _ws()
    child = parent.fork()
    child.ids["F9"] = "face:zz"
    child.indices["F9"] = 9
    child.labels["F1"]["name"] = "changed"
    child.history.append(apply_history(parent, _split_f1()).history[0])
    assert "F9" not in parent.ids and "F9" not in parent.indices
    assert parent.labels["F1"] == {"name": "top"}
    assert parent.history == []
    assert child is not parent and child.brep_path == parent.brep_path


def test_fork_allocates_independently_from_the_same_point() -> None:
    parent = _ws()
    a = apply_history(parent.fork(), _split_f1())
    b = apply_history(parent.fork(), _split_f1())
    assert sorted(a.ids) == sorted(b.ids)  # same lineage point -> same next ids, no leakage


def test_apply_history_maps_split_and_does_not_mutate_parent() -> None:
    parent = _ws()
    child = apply_history(parent, _split_f1())
    diff = child.history[-1]
    assert diff.op == "split" and diff.seconds == 0.25
    assert diff.modified == {"F1": ["F3", "F4"]}
    assert diff.removed == []
    assert child.ids["F2"] == "face:bb" and child.ids["E1"] == "edge:cc"  # untouched keep ids
    assert child.ids["F3"] == "face:a1" and child.ids["F4"] == "face:a2"
    assert "F1" not in child.ids
    assert parent.ids["F1"] == "face:aa" and parent.history == []
    assert child.brep_path == "/tmp/b.brep" and child.original == "/tmp/a.brep"
    assert "edge:new" in child.ids.values()  # the unattributed new edge still gets an id
    assert diff.params["unattributed"] == ["E2"]


def test_one_to_one_modified_keeps_the_id_and_refreshes_the_fingerprint() -> None:
    rec = _record(
        "trim",
        [_ent("face", 1, "face:aa-trimmed"), _ent("face", 2, "face:bb"),
         _ent("edge", 1, "edge:cc"), _ent("solid", 1, "solid:dd")],
        [_row("face", 1, now=0, modified=[_kid("face", 1)]), _row("face", 2),
         _row("edge", 1), _row("solid", 1)])
    child = apply_history(_ws(), rec)
    assert child.ids["F1"] == "face:aa-trimmed"
    assert child.history[-1].modified == {"F1": ["F1"]}
    assert child.labels["F1"] == {"name": "top"}
    assert child.resolve(["F1"]) == ["F1"]
    assert set(child.ids) == {"F1", "F2", "E1", "B1"}  # nothing minted


def test_one_to_one_modified_follows_a_renumbered_index() -> None:
    rec = _record(
        "heal",
        [_ent("face", 1, "face:bb"), _ent("face", 2, "face:aa2"),
         _ent("edge", 1, "edge:cc"), _ent("solid", 1, "solid:dd")],
        [_row("face", 1, now=0, modified=[_kid("face", 2)]), _row("face", 2, now=1),
         _row("edge", 1), _row("solid", 1)])
    child = apply_history(_ws(), rec)
    assert child.ids["F1"] == "face:aa2" and child.indices["F1"] == 2
    assert child.ids["F2"] == "face:bb" and child.indices["F2"] == 1


def test_many_to_one_merge_retires_parents_and_mints_one_child() -> None:
    rec = _record(
        "merge",
        [_ent("face", 1, "face:m"), _ent("edge", 1, "edge:cc"), _ent("solid", 1, "solid:dd")],
        [_row("face", 1, now=0, modified=[_kid("face", 1)]),
         _row("face", 2, now=0, modified=[_kid("face", 1)]), _row("edge", 1), _row("solid", 1)])
    child = apply_history(_ws(), rec)
    assert child.history[-1].modified == {"F1": ["F3"], "F2": ["F3"]}
    assert "F1" not in child.ids and "F2" not in child.ids and child.ids["F3"] == "face:m"
    assert child.labels["F3"] == {"name": "top"}  # first parent's label wins


def test_identical_fingerprints_do_not_misbind_ids() -> None:
    """F1 and F2 are coincident faces (same fingerprint). Removing the SECOND must retire F2."""
    ws = Workspace(brep_path="/tmp/a.brep", ids={"F1": "face:dup", "F2": "face:dup"},
                   labels={"F1": {"name": "one"}, "F2": {"name": "two"}}, history=[],
                   original="/tmp/a.brep", indices={"F1": 1, "F2": 2})
    rec = _record("cut", [_ent("face", 1, "face:dup")],
                  [_row("face", 1, fp="face:dup"),
                   _row("face", 2, fp="face:dup", now=0, removed=True)])
    child = apply_history(ws, rec)
    assert child.history[-1].removed == ["F2"]
    assert child.ids == {"F1": "face:dup"} and child.labels == {"F1": {"name": "one"}}
    with pytest.raises(UnknownTarget):
        child.resolve(["F2"])


def test_apply_history_refuses_a_workspace_without_kernel_indices() -> None:
    ws = Workspace(brep_path="/tmp/a.brep", ids={"F1": "face:aa"}, labels={}, history=[],
                   original="/tmp/a.brep")
    with pytest.raises(ValueError, match="kernel index"):
        apply_history(ws, _record("noop", [], []))


def test_old_format_record_fails_loudly() -> None:
    rec = _split_f1()
    del rec["version"]
    with pytest.raises(ValueError, match="version"):
        apply_history(_ws(), rec)
    rec = _split_f1()
    rec["version"] = 0
    with pytest.raises(ValueError, match="version"):
        apply_history(_ws(), rec)


def test_record_missing_keys_fails_loudly() -> None:
    rec = _split_f1()
    del rec["old_path"]
    with pytest.raises(ValueError, match="missing"):
        apply_history(_ws(), rec)


def test_record_computed_from_another_file_is_refused() -> None:
    rec = _split_f1()
    rec["old_path"] = "/tmp/other.brep"
    with pytest.raises(ValueError, match="stale workspace"):
        apply_history(_ws(), rec)


def test_record_for_a_different_shape_is_refused() -> None:
    rec = _split_f1()
    rec["history"][1]["fp"] = "face:not-bb"
    with pytest.raises(ValueError, match="different shape"):
        apply_history(_ws(), rec)
    rec = _split_f1()
    rec["history"] = [r for r in rec["history"] if r["type"] != "edge"]
    with pytest.raises(ValueError, match="no row for E1"):
        apply_history(_ws(), rec)


def test_labels_propagate_to_children_and_drop_with_removed() -> None:
    child = apply_history(_ws(), _split_f1())
    assert child.labels["F3"] == {"name": "top"} and child.labels["F4"] == {"name": "top"}
    child.labels["F3"]["name"] = "x"
    assert child.labels["F4"] == {"name": "top"}  # copies, not shared
    assert "F1" not in child.labels and child.labels["F2"] == {"name": "side"}


def test_generated_children_inherit_labels_and_removed_is_reported() -> None:
    rec = _record(
        "cut",
        [_ent("face", 1, "face:aa"), _ent("face", 2, "face:g"),
         _ent("edge", 1, "edge:cc"), _ent("solid", 1, "solid:dd")],
        [_row("face", 1, generated=[_kid("face", 2)]),
         _row("face", 2, now=0, removed=True), _row("edge", 1), _row("solid", 1)],
        declared_added=["F9"], declared_removed=["F2"])
    child = apply_history(_ws(), rec)
    assert child.history[-1].generated == {"F1": ["F3"]}
    assert child.history[-1].removed == ["F2"]
    assert child.history[-1].declared_added == ["F9"]
    assert child.history[-1].declared_removed == ["F2"]
    assert child.ids["F1"] == "face:aa" and "F2" not in child.ids
    assert child.labels["F3"] == {"name": "top"}


def test_ids_are_monotonic_across_ops_and_never_reused() -> None:
    first = apply_history(_ws(), _split_f1())  # F3, F4 and E2 minted
    rec = _record(
        "split",
        [_ent("face", 1, "face:a1x"), _ent("face", 2, "face:bb"), _ent("face", 3, "face:a2"),
         _ent("face", 4, "face:a1y"), _ent("edge", 1, "edge:cc"), _ent("edge", 2, "edge:new"),
         _ent("solid", 1, "solid:dd")],
        [_row("face", 1, fp="face:a1", now=0, modified=[_kid("face", 1), _kid("face", 4)]),
         _row("face", 2), _row("face", 3, fp="face:a2"), _row("edge", 1),
         _row("edge", 2, fp="edge:new"), _row("solid", 1)],
        path="/tmp/c.brep", old_path="/tmp/b.brep")
    second = apply_history(first, rec)
    # first.indices: F3->1, F2->2, F4->3 (kernel order); F3 splits into two new children
    assert second.history[-1].modified == {"F3": ["F5", "F6"]}
    assert {"F3", "F1"}.isdisjoint(second.ids)


def test_resolve_returns_live_ids() -> None:
    assert _ws().resolve(["F1", "E1"]) == ["F1", "E1"]


def test_resolve_removed_id_raises_unknown_target() -> None:
    child = apply_history(_ws(), _split_f1())
    with pytest.raises(UnknownTarget) as info:
        child.resolve(["F2", "F1"])
    assert info.value.id == "F1"
    assert "split" in info.value.reason and "F3" in info.value.reason and "F4" in info.value.reason


def test_resolve_distinguishes_removed_from_never_existed() -> None:
    rec = _record("cut", [_ent("face", 1, "face:aa"), _ent("edge", 1, "edge:cc"),
                          _ent("solid", 1, "solid:dd")],
                  [_row("face", 1), _row("face", 2, now=0, removed=True), _row("edge", 1),
                   _row("solid", 1)], path="/tmp/c.brep")
    child = apply_history(_ws(), rec)
    with pytest.raises(UnknownTarget) as removed:
        child.resolve(["F2"])
    with pytest.raises(UnknownTarget) as never:
        child.resolve(["F999"])
    assert "removed" in removed.value.reason and "cut" in removed.value.reason
    assert "never existed" in never.value.reason
    assert removed.value.reason != never.value.reason


def test_site_dataclass_shape() -> None:
    site = Site(id="s1", kind="proximity", entities=["F1"], xyz=[(0.0, 0.0, 0.0)],
                meshers_failing=["gmsh"], dist_over_h=0.2, graph={}, view_png=None)
    assert site.kind == "proximity" and site.view_png is None
