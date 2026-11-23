# Responsibility: Verify the v4 Workspace's identity rules without a kernel: fork independence,
#                 UnknownTarget on removed/replaced/unknown ids, monotonic ids, label inheritance.
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
    )


def _split_f1() -> dict:
    """F1 (face:aa) split in two by an op; everything else untouched."""
    return {
        "op": "split",
        "params": {"at": 0.5},
        "brep_path": "/tmp/b.brep",
        "seconds": 0.25,
        "declared_added": [],
        "declared_removed": [],
        "entities": [
            {"type": "face", "fp": "face:a1"}, {"type": "face", "fp": "face:a2"},
            {"type": "face", "fp": "face:bb"}, {"type": "edge", "fp": "edge:cc"},
            {"type": "edge", "fp": "edge:new"}, {"type": "solid", "fp": "solid:dd"},
        ],
        "history": [
            {"type": "face", "fp": "face:aa", "removed": False,
             "modified": [{"type": "face", "fp": "face:a1"}, {"type": "face", "fp": "face:a2"}],
             "generated": []},
        ],
    }


def test_fork_is_independent() -> None:
    parent = _ws()
    child = parent.fork()
    child.ids["F9"] = "face:zz"
    child.labels["F1"]["name"] = "changed"
    child.history.append(apply_history(parent, _split_f1()).history[0])
    assert "F9" not in parent.ids
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
    # the unattributed new edge still gets an id
    assert "edge:new" in child.ids.values()


def test_labels_propagate_to_children_and_drop_with_removed() -> None:
    child = apply_history(_ws(), _split_f1())
    assert child.labels["F3"] == {"name": "top"} and child.labels["F4"] == {"name": "top"}
    child.labels["F3"]["name"] = "x"
    assert child.labels["F4"] == {"name": "top"}  # copies, not shared
    assert "F1" not in child.labels and child.labels["F2"] == {"name": "side"}


def test_generated_children_inherit_labels_and_removed_is_reported() -> None:
    rec = {
        "op": "cut", "params": {}, "brep_path": "/tmp/c.brep", "seconds": 0.1,
        "declared_added": ["F9"], "declared_removed": ["F2"],
        "entities": [{"type": "face", "fp": "face:aa"}, {"type": "face", "fp": "face:g"},
                     {"type": "edge", "fp": "edge:cc"}, {"type": "solid", "fp": "solid:dd"}],
        "history": [
            {"type": "face", "fp": "face:aa", "removed": False, "modified": [],
             "generated": [{"type": "face", "fp": "face:g"}]},
            {"type": "face", "fp": "face:bb", "removed": True, "modified": [], "generated": []},
        ],
    }
    child = apply_history(_ws(), rec)
    assert child.history[-1].generated == {"F1": ["F3"]}
    assert child.history[-1].removed == ["F2"]
    assert child.history[-1].declared_added == ["F9"]
    assert child.history[-1].declared_removed == ["F2"]
    assert child.ids["F1"] == "face:aa" and "F2" not in child.ids
    assert child.labels["F3"] == {"name": "top"}


def test_ids_are_monotonic_across_ops_and_never_reused() -> None:
    first = apply_history(_ws(), _split_f1())  # F3, F4 and E2 minted
    rec = {
        "op": "split", "params": {}, "brep_path": "/tmp/c.brep", "seconds": 0.0,
        "declared_added": [], "declared_removed": [],
        "entities": [{"type": "face", "fp": "face:a1x"}, {"type": "face", "fp": "face:a2"},
                     {"type": "face", "fp": "face:bb"}, {"type": "edge", "fp": "edge:cc"},
                     {"type": "edge", "fp": "edge:new"}, {"type": "solid", "fp": "solid:dd"}],
        "history": [{"type": "face", "fp": "face:a1", "removed": False,
                     "modified": [{"type": "face", "fp": "face:a1x"}], "generated": []}],
    }
    second = apply_history(first, rec)
    assert second.history[-1].modified == {"F3": ["F5"]}  # counters are per kind
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
    rec = {
        "op": "cut", "params": {}, "brep_path": "/tmp/c.brep", "seconds": 0.0,
        "declared_added": [], "declared_removed": [],
        "entities": [{"type": "face", "fp": "face:aa"}, {"type": "edge", "fp": "edge:cc"},
                     {"type": "solid", "fp": "solid:dd"}],
        "history": [{"type": "face", "fp": "face:bb", "removed": True, "modified": [],
                     "generated": []}],
    }
    child = apply_history(_ws(), rec)
    with pytest.raises(UnknownTarget) as removed:
        child.resolve(["F2"])
    with pytest.raises(UnknownTarget) as never:
        child.resolve(["F999"])
    assert "removed" in removed.value.reason and "cut" in removed.value.reason
    assert "never existed" in never.value.reason or "unknown" in never.value.reason
    assert removed.value.reason != never.value.reason


def test_site_dataclass_shape() -> None:
    site = Site(id="s1", kind="proximity", entities=["F1"], xyz=[(0.0, 0.0, 0.0)],
                meshers_failing=["gmsh"], dist_over_h=0.2, graph={}, view_png=None)
    assert site.kind == "proximity" and site.view_png is None
