# Responsibility: Verify the v4 op registry and `validate_call` without a kernel: every split op is
#                 registered with bounded params, out-of-bounds params and unknown/removed/mis-kinded
#                 targets come back as messages (never a crash), and a new op registers with the
#                 decorator alone.
# Boundaries: hermetic - what the ops DO to a shape is proven natively in
#             tests/native/test_native_repair_v4_ops_split.py.
from __future__ import annotations

import math
from pathlib import Path

import pytest

from meshpipeline.cad.repair_v4 import ops
from meshpipeline.cad.repair_v4.ops import (
    REGISTRY,
    OpDeclined,
    OpFailed,
    register,
    run_kernel_op,
    validate_call,
)
from meshpipeline.cad.repair_v4.types import Site
from meshpipeline.cad.repair_v4.worker_pool import CallResult
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
    assert any("E1 is an edge, not a face" in e
               for e in validate_call("split_angle", ["E1"], {"max_deg": 90}, ws))
    assert any("F1 is a face, not an edge" in e for e in validate_call(
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


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_validate_call_rejects_non_finite_numbers(bad: float) -> None:
    errors = validate_call("split_by_proximity", [], {"face_pairs": [["F1", "F2"]],
                                                      "target_ratio": bad}, _ws())
    assert any("not a finite number" in e for e in errors), errors
    assert validate_call("split_angle", ["F1"], {"max_deg": bad}, _ws())


def test_unify_scope_rules_are_validate_call_errors_not_apply_surprises() -> None:
    ws = _ws()
    assert any("needs target faces" in e for e in validate_call(
        "unify_same_domain", [], {"scope": "targets", "keep": []}, ws))
    assert any("takes no targets" in e for e in validate_call(
        "unify_same_domain", ["F1"], {"scope": "all", "keep": []}, ws))
    assert validate_call("unify_same_domain", ["F1"], {"scope": "targets", "keep": []}, ws) == []
    with pytest.raises(ValueError, match="needs target faces"):  # apply refuses the same call
        REGISTRY["unify_same_domain"].apply(None, ws, [], {"scope": "targets", "keep": []},  # type: ignore[arg-type]
                                            timeout_s=1)


def test_a_schema_the_validator_cannot_enforce_is_refused_at_registration() -> None:
    def op_with(schema: dict) -> type:
        class _Bad:
            name = "_bad_op"
            params_schema = schema

            def applies_to(self, site: Site) -> bool:
                return False

            def apply(self, pool, ws, targets, params, *, timeout_s):  # type: ignore[no-untyped-def]
                raise NotImplementedError

        return _Bad

    for schema, needle in [
        ({"type": "object", "properties": {"x": {"type": "number", "exclusiveMinimum": 0}}},
         "exclusiveMinimum"),
        ({"type": "object", "properties": {"x": {"oneOf": []}}}, "oneOf"),
        ({"type": "object", "properties": {"x": {"type": "complex"}}}, "complex"),
    ]:
        with pytest.raises(ValueError, match=needle):
            register(op_with(schema))
    assert "_bad_op" not in REGISTRY


def test_union_and_null_types_are_checked_without_raising() -> None:
    @register
    class _Union:
        name = "_union_op"
        params_schema = {"type": "object",
                         "properties": {"v": {"type": ["string", "null"]}}}

        def applies_to(self, site: Site) -> bool:
            return False

        def apply(self, pool, ws, targets, params, *, timeout_s):  # type: ignore[no-untyped-def]
            raise NotImplementedError

    try:
        assert validate_call("_union_op", [], {"v": None}, _ws()) == []
        assert validate_call("_union_op", [], {"v": "a"}, _ws()) == []
        assert any("string or null" in e for e in validate_call("_union_op", [], {"v": 3}, _ws()))
    finally:
        del REGISTRY["_union_op"]


class _FakePool:
    """A pool whose call leaves a partial output file (as a killed worker would) and answers `result`."""

    def __init__(self, result: CallResult) -> None:
        self.result = result
        self.out_paths: list[str] = []

    def call(self, fn_name: str, payload: dict, *, timeout_s: float) -> CallResult:
        self.out_paths.append(payload["out_path"])
        Path(payload["out_path"]).write_text("partial")
        return self.result


@pytest.mark.parametrize("status", ["timeout", "crashed", "error"])
def test_a_failed_worker_raises_op_failed_and_leaves_no_orphan(tmp_path: Path, status: str) -> None:
    work = tmp_path / "work"
    work.mkdir()
    ws = _ws()
    ws.brep_path = str(work / "a.brep")
    pool = _FakePool(CallResult(ok=False, value=None, error="boom", status=status, seconds=1.0))  # type: ignore[arg-type]
    with pytest.raises(OpFailed) as caught:
        run_kernel_op(pool, ws, kernel_fn="x", op="x", payload={}, timeout_s=1)  # type: ignore[arg-type]
    assert caught.value.status == status
    assert not Path(pool.out_paths[0]).exists()
    assert list(work.iterdir()) == []


def test_a_declining_worker_raises_op_declined_and_leaves_no_orphan(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    ws = _ws()
    ws.brep_path = str(work / "a.brep")
    pool = _FakePool(CallResult(ok=True, value={"declined": "geometry_moved: x"}, error=None,
                                status="ok", seconds=0.1))
    with pytest.raises(OpDeclined, match="geometry_moved"):
        run_kernel_op(pool, ws, kernel_fn="x", op="x", payload={}, timeout_s=1)  # type: ignore[arg-type]
    assert list(work.iterdir()) == []


def test_every_module_of_the_ops_package_is_imported_by_discovery() -> None:
    import pkgutil

    for module in pkgutil.iter_modules(ops.__path__):
        assert f"{ops.__name__}.{module.name}" in __import__("sys").modules


@pytest.mark.parametrize("name, targets, params", [
    ("remove_small", ["F2"], {"max_width_mm": 0.01}),
    ("remove_small", ["E1", "F1"], {"max_width_mm": 0.01}),
    ("sew", ["F1", "F2"], {"tol_ladder": [0.001]}),
])
def test_an_any_kind_target_accepts_every_live_entity(name: str, targets: list, params: dict) -> None:
    """`x-targets: {"kind": "any"}` means any live entity. It was compared literally ("F2 is a face, not an any"), so
    remove_small (min 1 target) could NEVER be called and sew with targets never ran: the lane-C4 smoke diagnosis."""
    assert validate_call(name, targets, params, _ws()) == []


def test_an_any_kind_target_still_refuses_a_dead_one() -> None:
    errors = validate_call("remove_small", ["F9"], {"max_width_mm": 0.01}, _ws())
    assert len(errors) == 1 and "F9" in errors[0] and "not an any" not in errors[0]
