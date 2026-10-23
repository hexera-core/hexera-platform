# Responsibility: Pin the mesh benchmark runner's host-independent half: scenario discovery and
#                 strict validation, the per-scenario row shape, the sealed bundle round-trip, and
#                 the summary aggregation (success rate, quality distribution, cost, per-engine
#                 breakdown, engine-selection accuracy).
# Boundaries: hermetic - temp directories, stub executors, no mesh kernel, no git beyond passing a
#             fixed engine_commit in. The real executor's native half is exercised on real hosts,
#             never here.
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"{name}_under_test", REPO / "devtools" / "validation" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mb = _load("mesh_benchmark")
bb = _load("benchmark_bundle")


def _write_scenario(root: Path, name: str, *, source="part.stl", payload=None, **overrides):
    directory = root / name
    directory.mkdir(parents=True)
    (directory / source).write_bytes(b"solid fake\nendsolid fake\n")
    body = {"name": name, "source": source, "settings": {}, "timeout_seconds": 60.0}
    body.update(overrides)
    if payload is not None:
        body = payload
    (directory / "scenario.json").write_text(json.dumps(body))
    return directory


def _result(**overrides):
    base = {
        "status": "success",
        "selected_engine": "snappy",
        "engine_version": "openfoam-2412",
        "mesh_statistics": {"cells": 1000, "max_non_orthogonality": 42.0, "max_skewness": 1.1},
        "quality_criteria": [{"key": "max_non_orthogonality", "measured": 42.0, "passed": True,
                              "gating": True}],
        "solver_ready": True,
        "timings": {"mesh_seconds": 1.5},
        "deliverables": ["case/mesh_manifest.json"],
    }
    base.update(overrides)
    return base


def _run(root, out, scenarios_executor, **kwargs):
    defaults = {
        "track": "clean-geometry", "corpus_name": "unit fixtures",
        "corpus_provenance": "authored inline by the unit test",
        "redistribution_status": "internal", "hardware_class": "unit-test",
        "engine_commit": "deadbeef"}
    defaults.update(kwargs)
    return mb.run_benchmark(root, out, execute=scenarios_executor, log=lambda *_: None,
                            **defaults)


# ---------------------------------------------------------------- discovery and validation

def test_discovers_scenarios_sorted_and_loads_fields(tmp_path):
    _write_scenario(tmp_path, "b-box", expected_engine="gmsh",
                    settings={"engine": "gmsh"}, timeout_seconds=30)
    _write_scenario(tmp_path, "a-duct")
    scenarios = mb.load_scenarios(tmp_path)
    assert [s.run.name for s in scenarios] == ["a-duct", "b-box"]
    box = scenarios[1]
    assert box.expected_engine == "gmsh"
    assert box.run.settings == {"engine": "gmsh"}
    assert box.run.timeout_seconds == 30.0
    assert box.run.source_path.exists()


def test_scenario_name_must_match_nothing_but_itself_is_loaded(tmp_path):
    # a directory without scenario.json is not a scenario and is simply not discovered
    (tmp_path / "stray").mkdir()
    (tmp_path / "stray" / "notes.txt").write_text("not a scenario")
    _write_scenario(tmp_path, "real")
    assert [s.run.name for s in mb.load_scenarios(tmp_path)] == ["real"]


@pytest.mark.parametrize("mutation, match", [
    ({"drop": "name"}, "name"),
    ({"drop": "source"}, "source"),
    ({"drop": "settings"}, "settings"),
    ({"drop": "timeout_seconds"}, "timeout_seconds"),
    ({"set": {"settings": "fast"}}, "settings"),
    ({"set": {"timeout_seconds": 0}}, "timeout_seconds"),
    ({"set": {"timeout_seconds": -5}}, "timeout_seconds"),
    ({"set": {"source": "missing.stl"}}, "missing.stl"),
    ({"set": {"source": "../escape.stl"}}, "escape"),
    ({"set": {"surprise": True}}, "surprise"),
    # The name becomes the workspace directory the runner DELETES before copying the case in, so
    # an absolute name or a traversal would aim rmtree at somebody else's directory.
    ({"set": {"name": "/tmp/somebody-elses-work"}}, "single path segment"),
    ({"set": {"name": "../sibling"}}, "single path segment"),
    ({"set": {"name": "nested/below"}}, "single path segment"),
    ({"set": {"name": ".."}}, "single path segment"),
])
def test_refuses_malformed_scenario(tmp_path, mutation, match):
    body = {"name": "bad", "source": "part.stl", "settings": {}, "timeout_seconds": 60}
    if "drop" in mutation:
        body.pop(mutation["drop"])
    for key, value in mutation.get("set", {}).items():
        body[key] = value
    _write_scenario(tmp_path, "bad", payload=body)
    with pytest.raises(mb.ScenarioError, match=match):
        mb.load_scenarios(tmp_path)


def test_refuses_unparseable_scenario_json(tmp_path):
    directory = tmp_path / "broken"
    directory.mkdir()
    (directory / "scenario.json").write_text("{not json")
    with pytest.raises(mb.ScenarioError, match="broken"):
        mb.load_scenarios(tmp_path)


def test_refuses_empty_root(tmp_path):
    with pytest.raises(mb.ScenarioError, match="no scenario"):
        mb.load_scenarios(tmp_path)


# ---------------------------------------------------------------- integrity of the run

def test_executor_is_never_shown_the_expected_engine(tmp_path):
    _write_scenario(tmp_path, "s1", expected_engine="gmsh")
    seen = []

    def execute(run, scratch):
        seen.append(run)
        return _result()

    _run(tmp_path, tmp_path / "bundle", execute)
    (run,) = seen
    assert not hasattr(run, "expected_engine")
    assert "expected_engine" not in str(run)


# ---------------------------------------------------------------- row shape

def test_row_carries_the_manifest_fields_the_spec_names(tmp_path):
    _write_scenario(tmp_path, "s1", expected_engine="snappy")
    bundle = _run(tmp_path, tmp_path / "bundle", lambda run, scratch: _result())
    _, rows, _, _ = bb.load_bundle(bundle)
    (row,) = rows
    assert row["scenario"] == "s1"
    assert row["source"] == "part.stl"
    assert len(row["source_sha256"]) == 64
    assert row["status"] == "success"
    assert row["selected_engine"] == "snappy"
    assert row["engine_version"] == "openfoam-2412"
    assert row["settings"] == {}
    assert row["mesh_statistics"]["cells"] == 1000
    assert row["quality_criteria"][0]["key"] == "max_non_orthogonality"
    assert row["solver_ready"] is True
    assert row["timings"]["mesh_seconds"] == 1.5
    assert row["timings"]["wall_seconds"] >= 0
    assert row["deliverables"] == ["case/mesh_manifest.json"]
    # scoring-only: compared after the run, never fed into it
    assert row["expected_engine"] == "snappy"
    assert row["engine_match"] is True


def test_unavailable_executor_result_is_recorded_truthfully(tmp_path):
    _write_scenario(tmp_path, "s1")
    reason = "unavailable: meshpipeline is not importable on this host"
    bundle = _run(tmp_path, tmp_path / "bundle",
                  lambda run, scratch: {"status": reason})
    _, rows, summary, _ = bb.load_bundle(bundle)
    (row,) = rows
    assert row["status"] == reason
    assert row["solver_ready"] is None
    assert row["selected_engine"] is None
    assert summary["statuses"] == {"unavailable": 1}
    assert summary["mesh_success_rate"] == 0.0


def test_runner_refuses_an_executor_status_it_does_not_recognise(tmp_path):
    _write_scenario(tmp_path, "s1")
    with pytest.raises(ValueError, match="status"):
        _run(tmp_path, tmp_path / "bundle", lambda run, scratch: {"status": "probably fine"})


# ---------------------------------------------------------------- bundle round-trip

def test_bundle_round_trips_through_the_shared_loader(tmp_path):
    _write_scenario(tmp_path, "a-duct")
    _write_scenario(tmp_path, "b-box", expected_engine="gmsh")
    bundle = _run(tmp_path, tmp_path / "bundle", lambda run, scratch: _result())
    manifest, rows, summary, settings = bb.load_bundle(bundle)
    assert manifest["benchmark"] == "mesh-engine"
    assert manifest["track"] == "clean-geometry"
    assert manifest["engine_commit"] == "deadbeef"
    # every scenario input is hashed, keyed relative to the scenario root
    assert "a-duct/part.stl" in manifest["input_hashes"]
    assert "b-box/scenario.json" in manifest["input_hashes"]
    assert summary["row_count"] == 2
    assert [r["scenario"] for r in rows] == ["a-duct", "b-box"]
    assert settings["scenario_root"].endswith(str(tmp_path.name))


# ---------------------------------------------------------------- summary aggregation

def test_summary_aggregates_success_quality_cost_and_engines():
    rows = [
        {"scenario": "a", "status": "success", "selected_engine": "snappy",
         "expected_engine": "snappy", "engine_match": True, "solver_ready": True,
         "mesh_statistics": {"max_non_orthogonality": 40.0, "cells": 100},
         "timings": {"wall_seconds": 2.0}},
        {"scenario": "b", "status": "success", "selected_engine": "snappy",
         "expected_engine": "gmsh", "engine_match": False, "solver_ready": False,
         "mesh_statistics": {"max_non_orthogonality": 80.0, "cells": 300},
         "timings": {"wall_seconds": 4.0}},
        {"scenario": "c", "status": "failed: rc=1", "selected_engine": "gmsh",
         "expected_engine": "gmsh", "engine_match": True, "solver_ready": None,
         "mesh_statistics": {}, "timings": {"wall_seconds": 1.0}},
        {"scenario": "d", "status": "unavailable: no native runtime",
         "selected_engine": None, "expected_engine": None, "engine_match": None,
         "solver_ready": None, "mesh_statistics": {}, "timings": {"wall_seconds": 0.0}},
    ]
    summary = mb.summarise(rows)
    assert summary["scenarios"] == 4
    assert summary["statuses"] == {"success": 2, "failed": 1, "unavailable": 1}
    assert summary["mesh_success_rate"] == 0.5
    # distribution, not a pass/fail collapse
    dist = summary["quality_distribution"]["max_non_orthogonality"]
    assert dist == {"count": 2, "min": 40.0, "max": 80.0, "mean": 60.0, "median": 60.0}
    assert summary["solver_ready"] == {"ready": 1, "not_ready": 1, "unknown": 2}
    assert summary["cost"]["total_wall_seconds"] == 7.0
    assert summary["cost"]["mean_wall_seconds"] == 1.75
    per_engine = summary["per_engine"]
    assert per_engine["snappy"] == {"runs": 2, "successes": 2, "success_rate": 1.0,
                                    "mean_wall_seconds": 3.0}
    assert per_engine["gmsh"]["runs"] == 1
    assert per_engine["gmsh"]["successes"] == 0
    # selection accuracy scored only over rows that declared an expectation
    assert summary["engine_selection"] == {"scored": 3, "matched": 2,
                                           "accuracy": pytest.approx(2 / 3)}


def test_summary_of_no_rows_divides_nothing():
    summary = mb.summarise([])
    assert summary["scenarios"] == 0
    assert summary["mesh_success_rate"] is None
    assert summary["engine_selection"]["accuracy"] is None


def test_execute_refuses_a_workspace_outside_the_scratch_tree(tmp_path):
    """load_scenario refuses such a name, but execute_scenario is reachable with a hand-built run
    and the line after this check deletes whatever the name resolves to. A directory outside the
    scratch tree is never ours to remove, so the run refuses instead of reaching rmtree."""
    outsider = tmp_path / "precious"
    outsider.mkdir()
    (outsider / "keepme.txt").write_text("not the benchmark's to delete")
    case = tmp_path / "case"
    case.mkdir()
    source = tmp_path / "part.stl"
    source.write_bytes(b"solid fake\nendsolid fake\n")
    run = mb.ScenarioRun(name=str(outsider), directory=tmp_path, source_path=source,
                         settings={}, timeout_seconds=60.0, case_dir=case)

    result = mb.execute_scenario(run, tmp_path / "scratch")

    # named, so this cannot pass by bailing out earlier for some unrelated reason
    assert "resolves outside the scratch directory" in result["status"]
    assert (outsider / "keepme.txt").exists(), "the benchmark deleted a directory it was handed"
