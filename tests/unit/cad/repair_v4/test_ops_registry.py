# Responsibility: Verify the v4 op registry and `validate_call` without a kernel: every split op is
#                 registered with bounded params, out-of-bounds params and unknown/removed/mis-kinded
#                 targets come back as messages (never a crash), and a new op registers with the
#                 decorator alone.
# Boundaries: hermetic - what the ops DO to a shape is proven natively in
#             tests/native/test_native_repair_v4_ops_split.py.
from __future__ import annotations

import pytest

from meshpipeline.cad.repair_v4 import ops
from meshpipeline.cad.repair_v4.ops import REGISTRY, register, validate_call
from meshpipeline.cad.repair_v4.types import Site
from meshpipeline.cad.repair_v4.workspace import Diff, Workspace

SPLIT_OPS = ["split_by_proximity", "split_angle", "split_closed", "split_continuity",
             "unify_same_domain"]


def _ws() -> Workspace:
    return Workspace(
        brep_path="/tmp/a.brep",
        ids={"F1": "face:aa", "F2": "face:bb", "E1": "edge:cc", "B1": "solid:dd"},
        labels={}, history=[], original="/tmp/a.brep",
        indices={"F1": 1, "F2": 2, "E1": 1, "B1": 1})


def _site(kind: str) -> Site:
    return Site(id="s1", kind=kind, entities=["F1"], xyz=[], meshers_failing=[],  # type: ignore[arg-type]
                dist_over_h=None, graph={}, view_png=None)


def test_every_split_op_is_registered_with_a_bounded_schema() -> None:
    for name in SPLIT_OPS:
        op = REGISTRY[name]
        assert op.name == name
        assert op.params_schema["type"] == "object"
        assert op.params_schema["additionalProperties"] is False
    assert REGISTRY["split_angle"].params_schema["properties"]["max_deg"] == {
        "type": "number", "minimum": 30, "maximum": 180}
    assert REGISTRY["split_continuity"].params_schema["properties"]["order"]["enum"] == [1, 2]


def test_validate_call_accepts_a_good_call() -> None:
    ws = _ws()
    assert validate_call("split_angle", ["F1"], {"max_deg": 90}, ws) == []
    assert validate_call("split_by_proximity", [],
                         {"face_pairs": [["F1", "F2"]], "target_ratio": 0.25}, ws) == []
    assert validate_call("unify_same_domain", [], {"scope": "all", "keep": ["E1"]}, ws) == []


@pytest.mark.parametrize(("name", "targets", "params", "needle"), [
    ("split_angle", ["F1"], {"max_deg": 10}, "below the minimum 30"),
    ("split_angle", ["F1"], {"max_deg": 181}, "above the maximum 180"),
    ("split_angle", ["F1"], {"max_deg": "90"}, "expected number"),
    ("split_angle", ["F1"], {}, "missing required parameter 'max_deg'"),
    ("split_angle", ["F1"], {"max_deg": 90, "extra": 1}, "unknown parameter 'extra'"),
    ("split_angle", [], {"max_deg": 90}, "needs at least 1"),
    ("split_continuity", ["F1"], {"order": 3}, "is not one of [1, 2]"),
    ("split_continuity", ["F1"], {"order": 1.5}, "expected integer"),
    ("split_by_proximity", [], {"face_pairs": [["F1", "F2"]], "target_ratio": 0.01},
     "below the minimum 0.05"),
    ("split_by_proximity", [], {"face_pairs": [["F1", "F2"]], "target_ratio": 1.5},
     "above the maximum 1"),
    ("split_by_proximity", [], {"face_pairs": [["F1"]], "target_ratio": 0.5}, "at least 2 items"),
    ("split_by_proximity", [], {"face_pairs": [], "target_ratio": 0.5}, "at least 1 items"),
    ("unify_same_domain", [], {"scope": "everything", "keep": []}, "is not one of"),
])
def test_validate_call_rejects_out_of_bounds(name: str, targets: list, params: dict,
                                             needle: str) -> None:
    errors = validate_call(name, targets, params, _ws())
    assert any(needle in e for e in errors), errors


def test_validate_call_rejects_removed_target() -> None:
    ws = _ws()
    ws.history.append(Diff(op="split_angle", params={}, modified={}, generated={}, removed=["F2"],
                           declared_added=[], declared_removed=[], seconds=0.0))
    del ws.ids["F2"]
    errors = validate_call("split_angle", ["F2"], {"max_deg": 90}, ws)
    assert errors == ["targets[0]: F2: removed by op 'split_angle'; it no longer exists"]
    # ... and an id that was replaced by a split names its successors, wherever it appears
    ws.history.append(Diff(op="split_closed", params={}, modified={"F1": ["F7", "F8"]}, generated={},
                           removed=[], declared_added=[], declared_removed=[], seconds=0.0))
    del ws.ids["F1"]
    errors = validate_call("split_by_proximity", [], {"face_pairs": [["F1", "F9"]],
                                                      "target_ratio": 0.25}, ws)
    assert any("params.face_pairs[0][0]: F1: replaced by op 'split_closed' (now: F7, F8)" in e
               for e in errors), errors
    assert any("F9: unknown id" in e for e in errors), errors


def test_validate_call_rejects_wrong_kind_unknown_op_and_malformed_inputs() -> None:
    ws = _ws()
    assert any("E1 is a edge, not a face" in e
               for e in validate_call("split_angle", ["E1"], {"max_deg": 90}, ws))
    assert any("F1 is a face, not a edge" in e for e in validate_call(
        "unify_same_domain", [], {"scope": "all", "keep": ["F1"]}, ws))
    assert any("takes at most 0" in e for e in validate_call(
        "split_by_proximity", ["F1"], {"face_pairs": [["F1", "F2"]], "target_ratio": 0.5}, ws))
    assert "unknown op 'nope'" in validate_call("nope", [], {}, ws)[0]
    # garbage in never raises
    assert validate_call("split_angle", "F1", {"max_deg": 90}, ws)  # type: ignore[arg-type]
    assert validate_call("split_angle", ["F1"], None, ws)  # type: ignore[arg-type]
    assert validate_call("split_angle", [3], {"max_deg": True}, ws)  # type: ignore[list-item]


def test_applies_to_reads_the_site_kind() -> None:
    assert REGISTRY["split_by_proximity"].applies_to(_site("proximity"))
    assert not REGISTRY["split_by_proximity"].applies_to(_site("open"))
    assert REGISTRY["split_closed"].applies_to(_site("periodic"))


def test_a_new_op_registers_with_the_decorator_alone_and_duplicates_are_refused() -> None:
    @register
    class _Probe:
        name = "_probe_op"
        params_schema = {"type": "object", "properties": {"n": {"type": "integer", "minimum": 1}},
                         "additionalProperties": False}

        def applies_to(self, site: Site) -> bool:
            return False

        def apply(self, pool, ws, targets, params, *, timeout_s):  # type: ignore[no-untyped-def]
            raise NotImplementedError

    try:
        assert "_probe_op" in REGISTRY
        assert validate_call("_probe_op", [], {"n": 0}, _ws())  # the generic validator covers it
        assert validate_call("_probe_op", [], {"n": 2}, _ws()) == []
        with pytest.raises(ValueError, match="already registered"):
            register(_Probe)
    finally:
        del ops.REGISTRY["_probe_op"]


def test_kernel_functions_are_registered_for_the_workers() -> None:
    from meshpipeline.cad.repair_v4 import kernel_calls

    for fn in ("op_split_by_proximity", "op_split_angle", "op_split_closed",
               "op_split_continuity", "op_unify_same_domain"):
        assert fn in kernel_calls.REGISTRY
