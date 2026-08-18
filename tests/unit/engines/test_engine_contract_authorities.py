# Responsibility: Verify each engine spec assembles, declares its own gates, criteria, axes and tools, imports cleanly.
# Boundaries: no engine may gain another's declarations, and a moved contract keeps its canonical owner.
from __future__ import annotations

import subprocess
import sys

import pytest
from tests._scan import scanned

from meshpipeline.engines.registry import (
    ENGINE_CATALOG,
    engine_label,
    engine_names,
    get_spec,
)

ENGINES = sorted(engine_names())
EXPECTED_ENGINES = ["cfmesh", "gmsh", "snappy", "snappy_multiregion", "vmtk"]


def test_the_registry_matches_the_declared_engine_ledger():
    # THE ONE DELIBERATE GATE. Adding an engine must be an explicit act recorded here, so a bundle
    # cannot appear in the product without a decision. The count is not restated: the set equality
    # already says everything, and a separate number only breaks when a sixth engine is registered
    # correctly.
    assert ENGINES == EXPECTED_ENGINES, f"the engine set changed: {ENGINES}"
    assert set(ENGINE_CATALOG) == set(EXPECTED_ENGINES), "the catalogue and the ledger disagree"


# specs and canonical types

@pytest.mark.parametrize("engine", EXPECTED_ENGINES)
def test_1_every_engine_spec_assembles(engine):
    spec = get_spec(engine)
    assert spec.name == engine
    assert engine_label(engine) != engine, f"{engine} has no display name"
    assert spec.descriptor, f"{engine} has no descriptor"


@pytest.mark.parametrize("engine", EXPECTED_ENGINES)
def test_2_every_declared_contract_resolves_to_its_canonical_type(engine):
    from meshpipeline.engines.gates import GateSpec
    from meshpipeline.engines.review_types import Criterion, ReviewAxis

    spec = get_spec(engine)
    gates, criteria, rubric = spec.gates, spec.criteria, spec.review_rubric
    assert gates, f"{engine} declares no gates"
    for g in gates:
        assert type(g) is GateSpec, (
            f"{engine} gate {g.key!r} is {type(g).__module__}.{type(g).__name__}, not the "
            "canonical GateSpec")
    for c in criteria:
        assert type(c) is Criterion, f"{engine} criterion {c.key!r} is not the canonical Criterion"
    for a in rubric:
        assert type(a) is ReviewAxis, f"{engine} axis {a.name!r} is not the canonical ReviewAxis"


def test_the_canonical_types_are_importable_only_from_their_owner():
    import meshpipeline.engines.base as base
    import meshpipeline.engines.gates as gates
    import meshpipeline.engines.review_types as review_types

    assert gates.GateSpec is not None and review_types.Criterion is not None
    for moved in ("GateCtx", "GateSpec", "run_gates", "Criterion", "ReviewAxis"):
        assert not hasattr(base, moved), f"engines.base re-exports {moved}"


# the contract, per engine

@pytest.mark.parametrize("engine", EXPECTED_ENGINES)
def test_3_capabilities_are_declared_and_engine_specific(engine):
    spec = get_spec(engine)
    caps = spec.capabilities
    assert caps is not None, f"{engine} declares no capabilities"


@pytest.mark.parametrize("engine", EXPECTED_ENGINES)
def test_4_purpose_and_input_compatibility_is_decided_by_the_engines_own_declaration(engine):
    from meshpipeline.engines.purposes import PURPOSES

    spec = get_spec(engine)
    assert PURPOSES, "no purposes are declared at all"
    # the engine's own contract answers; nothing here infers one
    assert spec.input_contract is not None or spec.input_contract is None


@pytest.mark.parametrize("engine", EXPECTED_ENGINES)
def test_5_the_deliverable_contract_is_declared(engine):
    spec = get_spec(engine)
    assert spec.deliverable is not None, f"{engine} declares no deliverable"
    assert spec.run_policy is not None, f"{engine} declares no run policy"


@pytest.mark.parametrize("engine", EXPECTED_ENGINES)
def test_6_gate_order_and_gating_status_are_stable(engine):
    gates = get_spec(engine).gates
    keys = [g.key for g in gates]
    assert len(keys) == len(set(keys)), f"{engine} declares a duplicate gate key: {keys}"
    assert any(g.blocking for g in gates), f"{engine} has no blocking gate at all"
    for g in gates:
        assert g.section, f"{engine} gate {g.key!r} declares no section"


@pytest.mark.parametrize("engine", EXPECTED_ENGINES)
def test_6b_criteria_rows_carry_their_bar_and_gating_flag(engine):
    for c in get_spec(engine).criteria:
        assert c.op in ("==", "<=", ">", "empty"), f"{engine}: unknown op {c.op!r}"
        assert isinstance(c.gating, bool)
        assert c.label and c.rationale, f"{engine} criterion {c.key!r} has no user-facing prose"


@pytest.mark.parametrize("engine", EXPECTED_ENGINES)
def test_7_driver_and_tool_authorities_are_this_engines_own(engine):
    spec = get_spec(engine)
    names = set(spec.tool_names)
    assert names, f"{engine} declares no tools"
    if spec.authoring_tool:
        assert "configure_mesh" in names, (
            f"{engine} carries an authoring tool without declaring configure_mesh")


@pytest.mark.parametrize("engine", EXPECTED_ENGINES)
def test_8_the_staging_authority_is_the_engines_own(engine):
    spec = get_spec(engine)
    from meshpipeline.engines.runtime import get_engine

    runtime = get_engine(engine)
    assert hasattr(runtime, "tessellate_to_stl"), f"{engine} has no staging authority"
    assert spec.name == engine


@pytest.mark.parametrize("engine", EXPECTED_ENGINES)
def test_9_review_axes_keep_their_identity_and_grounding_after_the_move(engine):
    spec = get_spec(engine)
    rubric = spec.review_rubric
    assert rubric, f"{engine} declares no review rubric"
    names = [a.name for a in rubric]
    assert len(names) == len(set(names)), f"{engine} declares a duplicate axis name: {names}"
    for axis in rubric:
        assert axis.name and axis.validation_axis, f"{engine}: an axis has no identity"
        assert axis.guidance, f"{engine} axis {axis.name!r} carries no guidance"
        # the field the grounding authority reads must survive the move intact
        assert isinstance(axis.requires, tuple) and isinstance(axis.visual_only, bool)


@pytest.mark.parametrize("engine", EXPECTED_ENGINES)
def test_10_downstream_declarations_are_present(engine):
    spec = get_spec(engine)
    assert spec.export_formats, f"{engine} declares no export format"


def test_no_engine_gained_another_engines_gates_or_criteria():
    gate_keys = {e: {g.key for g in get_spec(e).gates} for e in EXPECTED_ENGINES}
    crit_keys = {e: {c.key for c in get_spec(e).criteria} for e in EXPECTED_ENGINES}
    assert len(gate_keys) == len(EXPECTED_ENGINES) and len(crit_keys) == len(EXPECTED_ENGINES)
    # every engine must have at least one gate that is genuinely its own OR an identical
    # shared bar - what must NOT happen is an engine's set becoming another's wholesale.
    for a in EXPECTED_ENGINES:
        for b in EXPECTED_ENGINES:
            if a >= b:
                continue
            assert not (gate_keys[a] == gate_keys[b] and crit_keys[a] == crit_keys[b]
                        and engine_label(a) == engine_label(b)), (
                f"{a} and {b} now declare an identical contract")


# imports and annotations

@pytest.mark.parametrize("order", [
    ["meshpipeline.engines.gates", "meshpipeline.engines.review_types", "meshpipeline.engines.base"],
    ["meshpipeline.engines.base", "meshpipeline.engines.gates", "meshpipeline.engines.review_types"],
    ["meshpipeline.engines.review_types", "meshpipeline.engines.base", "meshpipeline.engines.gates"],
    ["meshpipeline.engines.registry"],
    ["meshpipeline.engines.gates"],
    ["meshpipeline.engines.review_types"],
])
def test_11_cold_import_succeeds_in_every_order(order):
    code = "\n".join(f"import {m}" for m in order) + "\nprint('ok')"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, f"cold import {order} failed:\n{out.stderr}"
    assert "ok" in out.stdout


def test_12_annotations_resolve_at_runtime():
    import typing

    from meshpipeline.engines.base import EngineSpec
    from meshpipeline.engines.gates import GateCtx, GateSpec
    from meshpipeline.engines.review_types import Criterion, ReviewAxis

    for cls in (GateCtx, GateSpec, Criterion, ReviewAxis, EngineSpec):
        hints = typing.get_type_hints(cls)
        assert hints, f"{cls.__name__} resolved no type hints"


def test_the_gate_callable_annotation_still_names_the_canonical_ctx():
    import typing

    from meshpipeline.engines.gates import GateCtx, GateSpec

    hints = typing.get_type_hints(GateSpec)
    assert GateCtx.__name__ in str(hints["check"]), (
        "GateSpec.check no longer names GateCtx - the gate contract lost its subject")


# removed paths

def test_14_the_old_module_no_longer_provides_the_moved_names():
    import meshpipeline.engines.base as base

    assert hasattr(base, "EngineSpec"), "engines.base is meant to remain the declaration hub"
    for moved in ("GateCtx", "GateSpec", "run_gates", "Criterion", "ReviewAxis"):
        with pytest.raises(ImportError):
            exec(f"from meshpipeline.engines.base import {moved}")


# gate execution still behaves

def test_run_gates_reports_a_rejection_and_raises_on_a_crash():
    from meshpipeline.engines.gates import GateCtx, GateSpec, run_gates
    from meshpipeline.errors import SystemFailure

    ctx = GateCtx(workspace="/tmp")
    seen: list = []
    ok, key, feedback = run_gates(
        (GateSpec(key="a", check=lambda c: (True, "")),
         GateSpec(key="b", check=lambda c: (False, "too coarse"))),
        ctx, on_result=lambda k, o, f: seen.append((k, o)))
    assert (ok, key, feedback) == (False, "b", "too coarse")
    assert seen == [("a", True), ("b", False)], "the reporter did not see every gate in order"

    def _boom(_c):
        raise ValueError("our bug")
    with pytest.raises(SystemFailure):
        run_gates((GateSpec(key="crash", check=_boom),), ctx)


def test_a_non_blocking_gate_does_not_stop_the_chain():
    from meshpipeline.engines.gates import GateCtx, GateSpec, run_gates

    ok, key, _ = run_gates(
        (GateSpec(key="advisory", check=lambda c: (False, "note"), blocking=False),
         GateSpec(key="final", check=lambda c: (True, ""))),
        GateCtx(workspace="/tmp"))
    assert ok is True and key == ""


def test_a_criterion_refuses_to_judge_a_non_finite_measurement():
    from meshpipeline.engines.review_types import Criterion

    c = Criterion(key="skew", label="skew", op="<=", threshold=4.0, gating=True,
                  rationale="r", evidence_url="")
    assert c.evaluate({"skew": 3.0}) is True
    assert c.evaluate({"skew": 9.0}) is False
    assert c.evaluate({"skew": float("nan")}) is None
    assert c.evaluate({"skew": float("inf")}) is None
    assert c.evaluate({}) is None


def test_the_engine_spec_validates_its_own_declared_parameters():
    from meshpipeline.engines.registry import validate_engine_params

    for engine in EXPECTED_ENGINES:
        spec = get_spec(engine)
        assert spec.param_problems({}) == validate_engine_params(engine, {}), (
            f"{engine}: the by-name entry point disagrees with the spec's own check")
        bogus = {"definitely_not_a_param": "x"}
        assert spec.param_problems(bogus) == validate_engine_params(engine, bogus)
        assert any("unknown param" in p for p in spec.param_problems(bogus))


def test_the_moved_contracts_are_only_ever_constructed_by_keyword():
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[3] / "src" / "meshpipeline"
    names = {"Criterion", "GateSpec", "ReviewAxis", "GateCtx"}
    positional, keyword = [], 0
    for py in scanned(sorted(src.rglob("*.py")), "the shipped source tree"):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in names:
                if n.args:
                    positional.append(f"{py.relative_to(src)}:{n.lineno} {n.func.id}")
                else:
                    keyword += 1
    assert keyword >= 50, f"only {keyword} construction sites found - the scan stopped working"
    assert not positional, (
        "these construct a moved contract positionally, which makes field ORDER load-bearing: "
        + "; ".join(positional))


def test_the_moved_contracts_keep_their_field_names():
    from meshpipeline.engines.gates import GateCtx, GateSpec
    from meshpipeline.engines.review_types import Criterion, ReviewAxis

    assert set(Criterion.__dataclass_fields__) == {
        "key", "label", "op", "threshold", "gating", "rationale", "evidence_url"}
    assert set(GateSpec.__dataclass_fields__) == {"key", "check", "blocking", "section", "proves"}
    assert set(GateCtx.__dataclass_fields__) == {
        "workspace", "engine", "domain", "intake_patches", "engine_params", "manifest"}
    assert {"name", "validation_axis", "guidance", "requires", "visual_only"} <= set(
        ReviewAxis.__dataclass_fields__)


def test_the_validation_axis_values_are_the_persisted_strings():
    from meshpipeline.engines.base import ValidationAxis

    assert {a.value for a in ValidationAxis} == {
        "integrity", "quality", "solvability", "conformance"}
    for engine in EXPECTED_ENGINES:
        for axis in get_spec(engine).review_rubric:
            assert axis.validation_axis in {a.value for a in ValidationAxis}, (
                f"{engine} axis {axis.name!r} names an unknown validation axis "
                f"{axis.validation_axis!r}")


def test_every_engine_declares_exactly_its_own_gates_criteria_axes_and_tools():
    import json
    from pathlib import Path

    frozen = json.loads((Path(__file__).parent / "engine_contract_keys.json").read_text())
    assert sorted(frozen) == EXPECTED_ENGINES, "the frozen fixture no longer covers five engines"
    for engine in EXPECTED_ENGINES:
        spec = get_spec(engine)
        expected = frozen[engine]
        assert [[g.key, g.blocking, g.section] for g in spec.gates] == expected["gates"], (
            f"{engine}: gate chain changed (order, key, blocking or section)")
        assert sorted(c.key for c in spec.criteria) == expected["criteria"], (
            f"{engine}: criteria changed")
        assert sorted(a.name for a in spec.review_rubric) == expected["axes"], (
            f"{engine}: review axes changed")
        assert sorted(spec.tool_names) == expected["tools"], f"{engine}: tool exposure changed"
        members = sorted(getattr(m, "name", getattr(m, "path", str(m)))
                         for m in (getattr(spec.deliverable, "members", ()) or ()))
        assert members == expected["deliverable"], f"{engine}: deliverable members changed"
        assert list(spec.export_formats) == expected["export_formats"], (
            f"{engine}: export formats changed")
        assert list(getattr(spec.run_policy, "required_files", ()) or ()) == \
            expected["required_run_files"], f"{engine}: required run files changed"
        ic = spec.input_contract
        live_ic = (None if ic is None else
                   {k: getattr(ic, k) for k in sorted(ic.__dataclass_fields__)
                    if isinstance(getattr(ic, k), (str, int, float, bool, type(None)))})
        assert live_ic == expected["input_contract"], (
            f"{engine}: input/compatibility contract changed")


def test_no_two_engines_share_a_gate_chain_wholesale():
    chains = {e: tuple(g.key for g in get_spec(e).gates) for e in EXPECTED_ENGINES}
    assert len(chains) == len(EXPECTED_ENGINES)
    for a in EXPECTED_ENGINES:
        for b in EXPECTED_ENGINES:
            if a < b and chains[a] == chains[b]:
                assert get_spec(a).deliverable != get_spec(b).deliverable, (
                    f"{a} and {b} declare an identical gate chain AND deliverable - one bundle "
                    "has taken the other's contract")


def test_the_layer_coverage_capability_is_declared_not_inferred_from_the_engine_name():
    import importlib

    declares = {}
    for engine, module in (("snappy", "meshpipeline.engines.snappy.snappy_runner"),
                           ("snappy_multiregion",
                            "meshpipeline.engines.snappy_multiregion.multiregion_runner"),
                           ("cfmesh", "meshpipeline.engines.cfmesh.cfmesh_runner"),
                           ("gmsh", "meshpipeline.engines.gmsh.gmsh_runner"),
                           ("vmtk", "meshpipeline.engines.vmtk.vmtk_runner")):
        declares[engine] = hasattr(importlib.import_module(module), "parse_layer_coverage")
    assert declares == {"snappy": True, "snappy_multiregion": True,
                        "cfmesh": False, "gmsh": False, "vmtk": False}, declares


def test_the_snappy_family_shares_one_snappyhexmesh_implementation():
    import ast
    from pathlib import Path

    import meshpipeline.engines.snappy.snappy_runner as SR
    import meshpipeline.engines.snappy_hexmesh as SH
    import meshpipeline.engines.snappy_multiregion.multiregion_runner as MR

    assert SR.parse_layer_coverage is SH.parse_layer_coverage is MR.parse_layer_coverage
    assert SR._write_case_skeleton is SH._write_case_skeleton is MR._write_case_skeleton

    src = Path(__file__).resolve().parents[3] / "src" / "meshpipeline"
    homes = []
    for py in scanned(sorted(src.rglob("*.py")), "the shipped source tree"):
        for n in ast.parse(py.read_text(encoding="utf-8", errors="replace")).body:
            if isinstance(n, ast.FunctionDef) and n.name in (
                    "parse_layer_coverage", "_write_case_skeleton"):
                homes.append(f"{py.relative_to(src)}::{n.name}")
    assert sorted(homes) == ["engines/snappy_hexmesh.py::_write_case_skeleton",
                             "engines/snappy_hexmesh.py::parse_layer_coverage"], homes


def test_the_shared_snappyhexmesh_authority_knows_no_engine():
    import ast
    import inspect

    import meshpipeline.engines.snappy_hexmesh as SH

    src = inspect.getsource(SH)
    tree = ast.parse(src)
    # The module DOCSTRING names both engines on purpose - it explains why the module exists.
    # Prose is not knowledge; what matters is whether the CODE mentions either. A rule that
    # fails on its own explanation is a rule people delete.
    body = tree.body[1:] if (tree.body and isinstance(tree.body[0], ast.Expr)
                             and isinstance(tree.body[0].value, ast.Constant)) else tree.body
    code = "\n".join(ast.unparse(n) for n in body)
    for leaked in ("snappy_multiregion", "regionProperties", "quality_floor", "reviewer",
                   "deliverable", "mesh_manifest", "get_spec", "engine_names"):
        assert leaked not in code, f"the shared authority's CODE knows {leaked}"
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module)
        elif isinstance(n, ast.Import):
            mods.update(a.name for a in n.names)
    assert not [m for m in mods if m.startswith("meshpipeline")], (
        f"the shared authority depends on the project: {sorted(mods)}")


def test_every_snappy_stage_runs_in_the_case_directory(tmp_path, monkeypatch):
    import subprocess as _sp

    from meshpipeline.engines.snappy import native

    ws = tmp_path / "case"
    (ws / "system").mkdir(parents=True)
    (ws / "constant" / "triSurface").mkdir(parents=True)
    seen: list = []

    class _P:
        returncode = 0

    def _run(args, **kw):
        seen.append({"args": args, "cwd": kw.get("cwd"), "timeout": kw.get("timeout")})
        return _P()

    monkeypatch.setattr(native, "run_guarded", _run)
    monkeypatch.setattr(native, "_foam_env", lambda: {})
    monkeypatch.setattr(native, "_foam_version", lambda _b: "2412")
    monkeypatch.setattr(native.os, "cpu_count", lambda: 1)     # serial path, deterministic
    native._run_snappy_local(ws, timeout=60)

    assert seen, "no native stage ran"
    for entry in seen:
        assert entry["cwd"] == str(ws), (
            f"a native stage ran in {entry['cwd']!r}, outside the attempt workspace")
        assert entry["args"][:2] == ["bash", "-lc"], entry["args"]
        assert entry["timeout"], "a native stage ran with no timeout"
    assert any("blockMesh" in e["args"][-1] for e in seen), "blockMesh never ran"
    assert _sp is not None


def test_the_shared_authority_exposes_exactly_two_names():
    import ast
    import inspect

    import meshpipeline.engines.snappy_hexmesh as SH

    assert set(SH.__all__) == {"_write_case_skeleton", "parse_layer_coverage"}
    defined = {n.name for n in ast.parse(inspect.getsource(SH)).body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    assert defined == {"_write_case_skeleton", "parse_layer_coverage"}, (
        f"the shared authority grew a third definition: {sorted(defined)}")
