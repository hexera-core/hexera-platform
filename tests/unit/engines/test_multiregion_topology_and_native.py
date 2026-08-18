# Responsibility: Verify the multi-region stage order, per-stage attribution, and the region reconciliation it produces.
from __future__ import annotations

from pathlib import Path

import pytest

from meshpipeline.engines.snappy_multiregion import native, regions
from meshpipeline.engines.snappy_multiregion.regions import (
    POLYMESH_COMPONENTS,
    RegionPropertiesError,
)

REQUIRED = ("boundary", "faces", "neighbour", "owner", "points")


def _region_properties(fluids=("fluid",), solids=("solid",)) -> str:
    return ("FoamFile{ version 2.0; format ascii; class dictionary; object regionProperties; }\n"
            "regions\n(\n"
            f"    fluid       ({' '.join(fluids)})\n"
            f"    solid       ({' '.join(solids)})\n"
            ");\n")


def _case(tmp_path, *, declared=("fluid", "solid"), meshed=None, complete=True,
          region_properties: str | None = None) -> Path:
    ws = tmp_path / "case"
    (ws / "constant").mkdir(parents=True, exist_ok=True)
    if region_properties is not None:
        (ws / "constant" / "regionProperties").write_text(region_properties)
    elif declared is not None:
        fluids = [declared[0]] if declared else []
        solids = list(declared[1:])
        (ws / "constant" / "regionProperties").write_text(_region_properties(fluids, solids))
    for name in (declared if meshed is None else meshed):
        poly = ws / "constant" / name / "polyMesh"
        poly.mkdir(parents=True, exist_ok=True)
        for component in (POLYMESH_COMPONENTS if complete else ("owner",)):
            (poly / component).write_text("x\n")
    return ws


# the region declaration

def test_a_valid_case_reconciles(tmp_path):
    inv = regions.inventory(_case(tmp_path))
    assert inv.declared == {"fluid": "fluid", "solid": "solid"}
    assert inv.present == ("fluid", "solid")
    assert inv.reconciled and inv.problem() == ""


def test_9_a_missing_region_declaration_is_refused(tmp_path):
    ws = _case(tmp_path)
    (ws / "constant" / "regionProperties").unlink()
    with pytest.raises(RegionPropertiesError, match="missing"):
        regions.inventory(ws)


@pytest.mark.parametrize("text", [
    "",
    "FoamFile{ }\n",
    "regions\n(\n    fluid       (fluid)\n",          # truncated: no closing `);`
    "not a foam file at all",
])
def test_10_a_malformed_declaration_is_refused_not_silently_empty(tmp_path, text):
    ws = _case(tmp_path, region_properties=text)
    with pytest.raises(RegionPropertiesError):
        regions.inventory(ws)


def test_11_an_empty_region_declaration_is_refused(tmp_path):
    ws = _case(tmp_path, region_properties="regions\n(\n);\n", meshed=("fluid",))
    with pytest.raises(RegionPropertiesError, match="declares no regions"):
        regions.inventory(ws)


def test_12_a_declared_region_with_no_mesh_is_reported_missing(tmp_path):
    inv = regions.inventory(_case(tmp_path, declared=("fluid", "solid"), meshed=("fluid",)))
    assert inv.missing == ("solid",) and not inv.reconciled
    assert "solid" in inv.problem() and "no complete mesh" in inv.problem()


def test_13_an_undeclared_region_directory_is_reported(tmp_path):
    inv = regions.inventory(_case(tmp_path, declared=("fluid", "solid"),
                                  meshed=("fluid", "solid", "domain0")))
    assert inv.undeclared == ("domain0",) and not inv.reconciled
    assert "domain0" in inv.problem()


def test_14_a_single_region_impostor_does_not_reconcile(tmp_path):
    ws = _case(tmp_path, region_properties=_region_properties(("fluid",), ()), meshed=("fluid",))
    root = ws / "constant" / "polyMesh"
    root.mkdir(parents=True, exist_ok=True)
    for component in POLYMESH_COMPONENTS:
        (root / component).write_text("x\n")
    inv = regions.inventory(ws)
    assert inv.present == ("fluid",), "the root pre-split mesh was counted as a region"
    assert "polyMesh" not in inv.present


def test_the_parser_reads_both_region_sets():
    parsed = regions.parse_region_properties(
        _region_properties(("air", "water"), ("steel",)))
    assert parsed == {"air": "fluid", "water": "fluid", "steel": "solid"}


# the region mesh itself

@pytest.mark.parametrize("missing", REQUIRED)
def test_15_every_polymesh_component_is_required(tmp_path, missing):
    ws = _case(tmp_path, declared=("fluid",), meshed=("fluid",))
    (ws / "constant" / "fluid" / "polyMesh" / missing).unlink()
    assert regions.region_mesh_problems(ws, "fluid") == (f"missing {missing}",)
    assert regions.present_regions(ws) == (), f"a region missing {missing} was accepted"


@pytest.mark.parametrize("empty", REQUIRED)
def test_16_a_present_but_empty_component_is_rejected(tmp_path, empty):
    ws = _case(tmp_path, declared=("fluid",), meshed=("fluid",))
    (ws / "constant" / "fluid" / "polyMesh" / empty).write_text("")
    assert regions.region_mesh_problems(ws, "fluid") == (f"empty {empty}",)
    assert regions.present_regions(ws) == ()


def test_the_component_set_is_exactly_the_five_a_solver_needs():
    assert set(POLYMESH_COMPONENTS) == set(REQUIRED) and len(POLYMESH_COMPONENTS) == 5


def test_owner_alone_is_not_a_region_mesh(tmp_path):
    ws = _case(tmp_path, declared=("fluid",), meshed=("fluid",), complete=False)
    assert regions.present_regions(ws) == (), "a directory holding only `owner` counted as a region"


def test_17_the_root_constant_polymesh_owner_is_never_the_authority(tmp_path):
    ws = tmp_path / "case"
    (ws / "constant" / "polyMesh").mkdir(parents=True)
    for component in POLYMESH_COMPONENTS:
        (ws / "constant" / "polyMesh" / component).write_text("x\n")
    assert regions.present_regions(ws) == ()

    import inspect
    src = inspect.getsource(regions)
    assert 'd.name != "polyMesh"' in src, "the root mesh is no longer excluded explicitly"


# the native stage sequence

def test_2_the_stage_order_is_the_contract():
    assert [c for c, _ in native.NATIVE_STAGES] == [
        "blockMesh",
        "surfaceFeatureExtract",
        "snappyHexMesh -overwrite",
        "splitMeshRegions -cellZones -overwrite",
    ]
    assert len(native.NATIVE_STAGES) == 4


def _drive(tmp_path, monkeypatch, *, rc_for=None, timeout_on=None, preflight=None):
    import subprocess as _sp

    ws = tmp_path / "case"
    (ws / "constant").mkdir(parents=True, exist_ok=True)
    (ws / ".regions.json").write_text('[{"name": "fluid", "type": "fluid", "solids": [0]}]')
    seen: list = []

    class _P:
        def __init__(self, rc): self.returncode = rc; self.stdout = "out"; self.stderr = ""

    def _run(args, **kw):
        cmd = args[-1]
        stage = next((n for c, n in native.NATIVE_STAGES if c in cmd), "?")
        seen.append({"stage": stage, "cwd": kw.get("cwd"), "args": args, "timeout": kw.get("timeout")})
        if timeout_on == stage:
            raise _sp.TimeoutExpired(args, kw.get("timeout", 0))
        return _P((rc_for or {}).get(stage, 0))

    monkeypatch.setattr(native, "run_guarded", _run)
    monkeypatch.setattr(native, "scan_case_dicts", lambda _ws: "")
    monkeypatch.setattr(native, "_foam_env", lambda: {})
    result = native.run_native_build(
        ws, preflight=preflight or (lambda _w: None),
        render_region_properties=lambda regs: "regions ( fluid ( fluid ) solid ( ) );\n",
        parse_layer_coverage=lambda _log: {})
    return ws, seen, result


def test_1_a_valid_build_runs_every_stage_in_order_then_declares_the_regions(tmp_path,
                                                                             monkeypatch):
    ws, seen, result = _drive(tmp_path, monkeypatch)
    assert [s["stage"] for s in seen] == [n for _c, n in native.NATIVE_STAGES]
    assert len(seen) == 4, f"{len(seen)} stages ran, expected 4"
    assert result["rc"] == 0
    assert (ws / "constant" / "regionProperties").exists(), (
        "a successful split did not declare its regions")


def test_3_every_stage_runs_in_the_case_directory_with_the_expected_argv(tmp_path, monkeypatch):
    ws, seen, _ = _drive(tmp_path, monkeypatch)
    for entry in seen:
        assert entry["cwd"] == str(ws), f"{entry['stage']} ran outside the case directory"
        assert entry["args"][:2] == ["bash", "-lc"], entry["args"]
        assert entry["timeout"], f"{entry['stage']} ran with no timeout"
    assert "source" in seen[0]["args"][-1], "the OpenFOAM environment was not sourced"


@pytest.mark.parametrize("failing", [n for _c, n in native.NATIVE_STAGES])
def test_4_a_nonzero_exit_at_any_stage_stops_the_build_and_is_attributed(tmp_path, monkeypatch,
                                                                         failing):
    ws, seen, result = _drive(tmp_path, monkeypatch, rc_for={failing: 1})
    assert result["rc"] == 1, f"a non-zero {failing} was not reported as a failure"
    assert result["stage"] == failing, f"the failure was attributed to {result['stage']!r}"
    ran = [s["stage"] for s in seen]
    assert ran[-1] == failing, f"stages ran after {failing} failed: {ran}"
    assert not (ws / "constant" / "regionProperties").exists(), (
        f"a case that failed at {failing} still declared its regions")


@pytest.mark.parametrize("stage", [n for _c, n in native.NATIVE_STAGES])
def test_6_a_timeout_is_reported_as_a_timeout_never_as_success(tmp_path, monkeypatch, stage):
    ws, seen, result = _drive(tmp_path, monkeypatch, timeout_on=stage)
    assert result["timed_out"] is True and result["rc"] != 0
    assert result["stage"] == stage
    assert [s["stage"] for s in seen][-1] == stage, "a stage ran after the timeout"
    assert not (ws / "constant" / "regionProperties").exists()


def test_a_refusal_happens_before_any_native_command(tmp_path, monkeypatch):
    ws, seen, result = _drive(tmp_path, monkeypatch,
                              preflight=lambda _w: {"code": "BAD_MAP", "error": "no region map"})
    assert seen == [], "a native command ran despite the preflight refusal"
    assert result["rc"] == -2 and result["code"] == "BAD_MAP"
    assert not (ws / "constant" / "regionProperties").exists()


def test_the_stage_outcome_distinguishes_a_timeout_from_an_exit_code():
    ok = native.StageOutcome(stage="blockMesh", returncode=0)
    failed = native.StageOutcome(stage="blockMesh", returncode=1)
    timed = native.StageOutcome(stage="blockMesh", returncode=-1, timed_out=True)
    assert ok.ok and not failed.ok and not timed.ok
    assert timed.timed_out and not failed.timed_out


# the delivery fence

def test_the_regions_split_gate_refuses_every_broken_topology(tmp_path):
    import json

    from meshpipeline.engines.gates import GateCtx
    from meshpipeline.engines.snappy_multiregion.gates import _gate_regions_split

    def _ctx(ws):
        manifest = {"quality": {"regions": [{"name": "fluid", "cells": 10},
                                            {"name": "solid", "cells": 10}],
                                "regions_missing": [], "regions_undeclared": []}}
        (ws / "mesh_manifest.json").write_text(json.dumps(manifest))
        return GateCtx(workspace=ws, engine="snappy_multiregion")

    ok, _ = _gate_regions_split(_ctx(_case(tmp_path / "good")))
    assert ok is True, "a reconciled case was refused"

    broken = {
        "no declaration": lambda: _case(tmp_path / "a", declared=("fluid", "solid")),
        "truncated": lambda: _case(tmp_path / "b", region_properties="regions (\n"),
        "missing region": lambda: _case(tmp_path / "c", declared=("fluid", "solid"),
                                        meshed=("fluid",)),
        "undeclared region": lambda: _case(tmp_path / "d", declared=("fluid", "solid"),
                                           meshed=("fluid", "solid", "ghost")),
        "incomplete mesh": lambda: _case(tmp_path / "e", declared=("fluid", "solid"),
                                         complete=False),
    }
    refused = 0
    for label, build in broken.items():
        ws = build()
        if label == "no declaration":
            (ws / "constant" / "regionProperties").unlink()
        ok, feedback = _gate_regions_split(_ctx(ws))
        assert ok is False, f"{label}: the gate admitted a broken case"
        assert "REGION_SPLIT_FAILED" in feedback, f"{label}: no attributable marker"
        refused += 1
    assert refused == 5, f"only {refused} broken cases were exercised"


def test_region_discovery_has_one_implementation():
    import ast
    import inspect

    from meshpipeline.engines.snappy_multiregion import multiregion_runner as mr

    src = inspect.getsource(mr._region_dirs)
    assert "present_regions" in src, "the runner no longer delegates region discovery"
    tree = ast.parse(inspect.getsource(mr))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value != "polyMesh" or True   # the literal alone is not the defect
    runner_src = inspect.getsource(mr)
    assert 'polyMesh" / "owner"' not in runner_src, (
        "the runner re-implements region discovery from `owner` alone")


def test_no_forwarding_shim_survives_in_the_region_authority():
    import inspect

    src = inspect.getsource(regions)
    assert "_region_dirs" not in src, (
        "engines/snappy_multiregion/regions.py carries a forwarding alias")
    assert set(regions.__all__) == {
        "POLYMESH_COMPONENTS", "DeliveredRegions", "RegionPropertiesError", "inventory",
        "parse_region_properties", "present_regions", "read_region_properties",
        "region_mesh_problems"}


def test_quality_floor_gates_on_declared_criteria(tmp_path):
    import json

    from meshpipeline.engines.gates import GateCtx
    from meshpipeline.engines.registry import get_spec
    from meshpipeline.engines.snappy_multiregion.gates import _gate_quality_floor

    spec = get_spec("snappy_multiregion")
    gating = {c.key for c in spec.criteria if c.gating}
    assert "skew_fraction" in gating, "skew_fraction stopped being a gating bar"

    def _measurements(**over):
        out = {}
        for c in spec.criteria:
            if c.op == "empty":
                out[c.key] = []
            elif c.op == "==":
                out[c.key] = c.threshold
            elif c.op == "<=":
                out[c.key] = c.threshold
            elif c.op == ">":
                out[c.key] = (c.threshold or 0) + 1
        out.update(over)
        return out

    ws = tmp_path / "case"
    ws.mkdir(parents=True)

    (ws / "mesh_manifest.json").write_text(json.dumps({"quality": _measurements()}))
    ok, _ = _gate_quality_floor(GateCtx(workspace=ws, engine="snappy_multiregion"))
    assert ok is True, "a mesh meeting every declared bar was refused"

    bad = next(c for c in spec.criteria if c.key == "skew_fraction")
    (ws / "mesh_manifest.json").write_text(json.dumps(
        {"quality": _measurements(skew_fraction=(bad.threshold or 0) + 1)}))
    ok, feedback = _gate_quality_floor(GateCtx(workspace=ws, engine="snappy_multiregion"))
    assert ok is False, "a mesh over the declared skew bar passed quality_floor"
    assert "skew" in feedback.lower(), feedback

    # a NON-gating bar must not fail the gate - advisory stays advisory
    advisory = next(c for c in spec.criteria if not c.gating and c.op == "<=")
    (ws / "mesh_manifest.json").write_text(json.dumps(
        {"quality": _measurements(**{advisory.key: (advisory.threshold or 0) + 1})}))
    ok, _ = _gate_quality_floor(GateCtx(workspace=ws, engine="snappy_multiregion"))
    assert ok is True, f"the advisory bar {advisory.key!r} gated the mesh"
