#!/usr/bin/env python3
# Responsibility: Run the mesh-engine benchmark over a directory of frozen scenarios and seal the
#                 result as a publication bundle. Owns: scenario discovery and strict validation,
#                 the per-scenario row shape, and the summary computed from rows.
# Boundaries: discovery, validation, row building, aggregation and sealing are pure files-and-dicts
#             and run on any host. Everything that touches the mesh kernel lives behind one
#             injectable `execute` callable; its default (`execute_scenario`) imports meshpipeline
#             lazily and reports an honest `unavailable:` status wherever this host cannot run the
#             native path - it never fabricates a mesh result.

# WHY THIS EXISTS.
#
# docs/research/public-benchmarks.md (First Implementation Slice, item 3) asks for a mesh benchmark
# runner that consumes frozen scenarios and emits the same bundle shape as the CAD repair runner:
# per-row manifest fields (source hash, selected engine, engine version, settings, mesh statistics,
# quality criteria, solver-readiness verdict, timings, deliverables) and an aggregate that reports
# the quality DISTRIBUTION rather than a single pass/fail score. benchmark_bundle.py owns the
# envelope; this module owns what a mesh row and a mesh summary contain.
#
# INTEGRITY RULE. Nothing in the run may read expected results to influence the run. A scenario's
# `expected_engine` exists only to score selection accuracy after the fact, so the executor is
# handed a `ScenarioRun` that structurally does not carry it; the comparison happens in
# `build_row`, after the executor has returned.
"""The frozen scenario format.

A scenario is one directory under the scenario root. It contains a `scenario.json` plus the
geometry file(s) it names; nothing in it may point outside its own directory. `scenario.json`
is a single JSON object with exactly these keys:

  name             (required) str   - the scenario's name; by convention the directory name.
  source           (required) str   - path of the source CAD/STL file, relative to the scenario
                                      directory; the file must exist inside it.
  settings         (required) dict  - the frozen run settings, passed to the executor verbatim.
                                      An `engine` key here pins the engine; absent, the pipeline's
                                      selection chooses.
  timeout_seconds  (required) num   - positive wall-clock budget for the native run.
  expected_engine  (optional) str   - the engine selection is EXPECTED to choose. Scoring only:
                                      it is never shown to the executor (see integrity rule above).
  case             (optional) str   - relative path of a frozen, pre-authored engine case
                                      directory. Without one the default executor cannot run the
                                      native mesher (authoring a case needs the full pipeline) and
                                      records the row as unavailable.

Any missing required key, any unknown key, a non-dict `settings`, a non-positive timeout, or a
`source`/`case` that escapes or is absent from the scenario directory refuses the WHOLE run with a
ScenarioError: a frozen corpus with a malformed scenario is not a corpus to publish from.
"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchmark_bundle import (  # noqa: E402
    BundleManifest,
    BundleWriter,
    current_engine_commit,
    hash_inputs,
    load_bundle,
    sha256_file,
)

SCENARIO_FILE = "scenario.json"
_REQUIRED = ("name", "source", "settings", "timeout_seconds")
_OPTIONAL = ("expected_engine", "case")

#: Terminal statuses a row may carry. Everything after the colon is the executor's honest reason.
STATUS_CLASSES = ("success", "failed", "unavailable")


class ScenarioError(ValueError):
    """A scenario directory that cannot be benchmarked; the message names it and the field."""


@dataclass(frozen=True)
class ScenarioRun:
    """What the executor is allowed to see: everything about the input, nothing about the answer."""

    name: str
    directory: Path
    source_path: Path
    settings: dict
    timeout_seconds: float
    case_dir: Path | None = None


@dataclass(frozen=True)
class Scenario:
    """A validated frozen scenario: the run view, plus the scoring-only expectation."""

    run: ScenarioRun
    expected_engine: str | None = None


def _inside(directory: Path, relative: str, field: str, name: str) -> Path:
    path = (directory / relative).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise ScenarioError(f"scenario {name!r}: {field} {relative!r} escapes the scenario directory")
    if not path.is_file() and not path.is_dir():
        raise ScenarioError(f"scenario {name!r}: {field} {relative!r} does not exist")
    return path


def load_scenario(directory: Path) -> Scenario:
    """Read and strictly validate one scenario directory. Refusal is a ScenarioError."""
    directory = Path(directory).resolve()  # members are resolved; /tmp-style symlinks must agree
    spec_path = directory / SCENARIO_FILE
    try:
        body = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScenarioError(f"{spec_path} is unreadable: {exc}") from exc
    if not isinstance(body, dict):
        raise ScenarioError(f"{spec_path} must hold a JSON object")
    missing = [key for key in _REQUIRED if key not in body]
    if missing:
        raise ScenarioError(f"{spec_path} is missing required field(s) {missing}")
    unknown = [key for key in body if key not in _REQUIRED + _OPTIONAL]
    if unknown:
        raise ScenarioError(f"{spec_path} carries unknown field(s) {unknown}")
    name = body["name"]
    if not isinstance(name, str) or not name:
        raise ScenarioError(f"{spec_path}: name must be a non-empty string")
    # THE NAME BECOMES A DIRECTORY, and the runner DELETES that directory before copying the case
    # into it. `scratch / name` with an absolute name is the absolute path, not a child of
    # scratch - so a scenario naming itself "/home/someone/work" would have the benchmark delete
    # it. One path segment, and not a traversal: the same containment rule `source` and `case`
    # already answer to, applied to the one field that reaches rmtree.
    if name != Path(name).name or name in (".", ".."):
        raise ScenarioError(
            f"{spec_path}: name {name!r} must be a single path segment - it names the run's "
            f"workspace directory")
    if not isinstance(body["settings"], dict):
        raise ScenarioError(f"{spec_path}: settings must be a JSON object")
    timeout = body["timeout_seconds"]
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ScenarioError(f"{spec_path}: timeout_seconds must be a positive number")
    source = _inside(directory, str(body["source"]), "source", name)
    if not source.is_file():
        raise ScenarioError(f"scenario {name!r}: source {body['source']!r} is not a file")
    case_dir = None
    if body.get("case") is not None:
        case_dir = _inside(directory, str(body["case"]), "case", name)
        if not case_dir.is_dir():
            raise ScenarioError(f"scenario {name!r}: case {body['case']!r} is not a directory")
    expected = body.get("expected_engine")
    if expected is not None and (not isinstance(expected, str) or not expected):
        raise ScenarioError(f"{spec_path}: expected_engine must be a non-empty string when given")
    return Scenario(
        run=ScenarioRun(name=name, directory=directory, source_path=source,
                        settings=dict(body["settings"]), timeout_seconds=float(timeout),
                        case_dir=case_dir),
        expected_engine=expected)


def load_scenarios(root: Path) -> list[Scenario]:
    """Discover and validate every scenario under `root`, fail-fast and sorted by directory.

    All scenarios are validated BEFORE any is run: a corpus with one malformed member is refused
    whole rather than benchmarked in part.
    """
    root = Path(root).resolve()
    directories = sorted(path.parent for path in root.rglob(SCENARIO_FILE))
    if not directories:
        raise ScenarioError(f"no scenario found under {root} (no {SCENARIO_FILE})")
    return [load_scenario(directory) for directory in directories]


# ---------------------------------------------------------------------------- the executor seam


def _unavailable(reason: str) -> dict:
    return {"status": f"unavailable: {reason}"}


def execute_scenario(run: ScenarioRun, scratch: Path) -> dict:
    """The default, kernel-invoking executor: drive the pipeline's mesh path directly, the way
    repair_corpus.py drives the repair loop - no database, no job, a scratch workspace.

    Honesty over coverage: engine selection is resolved from the frozen settings (an `engine` pin,
    else the registry default); the native mesher runs only over a frozen pre-authored `case`
    directory, because authoring a case needs the full pipeline (builder, renderer, approval
    contract). Anything this host cannot do comes back as `unavailable: <reason>`, never as a
    fabricated result.
    """
    try:
        from meshpipeline.engines.registry import default_engine, get_spec
    except Exception as exc:  # noqa: BLE001 - truthfully: this host has no engine stack
        return _unavailable(f"meshpipeline is not importable on this host ({exc})")

    pinned = run.settings.get("engine")
    try:
        engine = str(pinned) if pinned else default_engine()
        get_spec(engine)  # an unknown pin must not reach the dispatcher
    except Exception as exc:  # noqa: BLE001
        return {"status": f"failed: engine {pinned!r} is unknown to the registry ({exc})",
                "selected_engine": None}

    if run.case_dir is None:
        return {**_unavailable(
            "no frozen `case` directory: this host cannot author an engine case (that takes the "
            "full pipeline's builder and renderer); freeze one into the scenario or run inside "
            "the mesh image"), "selected_engine": engine}

    # load_scenario refuses a name that is not one path segment, but this function is reachable
    # with a hand-built ScenarioRun, and the next line deletes whatever it resolves to. A
    # directory outside the scratch tree is never ours to remove.
    workspace = (scratch / run.name).resolve()
    if not workspace.is_relative_to(Path(scratch).resolve()):
        return {"status": f"failed: scenario name {run.name!r} resolves outside the scratch "
                          f"directory, so its workspace cannot be prepared",
                "selected_engine": engine}
    if workspace.exists():
        shutil.rmtree(workspace)
    shutil.copytree(run.case_dir, workspace)
    shutil.copy2(run.source_path, workspace / run.source_path.name)
    # A FROZEN CASE MAY CARRY A MANIFEST from whenever it was authored. It just came along in the
    # copytree, and everything below reads mesh_manifest.json as THIS run's evidence - quality,
    # cell count, engine version. Remove it, so what is scored was written by the run being
    # scored or is honestly absent.
    stale_manifest = workspace / "mesh_manifest.json"
    if stale_manifest.exists():
        stale_manifest.unlink()

    started = time.monotonic()
    try:
        from meshpipeline.engines.dispatch import run_engine_local
        out = run_engine_local(workspace, engine=engine, timeout=int(run.timeout_seconds))
    except (ImportError, ModuleNotFoundError, FileNotFoundError) as exc:
        return {**_unavailable(f"native run cannot start on this host ({exc})"),
                "selected_engine": engine}
    except Exception as exc:  # noqa: BLE001 - a crash is a real benchmark outcome
        return {"status": f"failed: {type(exc).__name__}: {exc}", "selected_engine": engine}
    mesh_seconds = round(time.monotonic() - started, 2)

    result: dict = {"selected_engine": engine, "timings": {"mesh_seconds": mesh_seconds}}
    rc = out.get("rc") if isinstance(out, dict) else None
    manifest_path = workspace / "mesh_manifest.json"
    manifest: dict = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            return {**result, "status": f"failed: mesh_manifest.json is unreadable ({exc})"}
    if manifest:
        from meshpipeline.engines.quality_criteria import evaluate, measurements_from_manifest
        quality_criteria = manifest.get("quality_criteria") or {}
        result.update(
            engine_version=(manifest.get("engine_version") or None),
            mesh_statistics={**(manifest.get("quality") or {}),
                             "cells": manifest.get("cell_count")},
            quality_criteria=(quality_criteria.get("criteria")
                              or evaluate(engine, measurements_from_manifest(manifest))),
            solver_ready=quality_criteria.get("production_grade"),
            deliverables=sorted(str(p) for p in (manifest.get("mesh_paths") or {}).values()
                                if p and Path(p).exists()) + [str(manifest_path)])
    delivered = bool(manifest) and bool(manifest.get("mesh_written"))
    if rc == 0 and delivered:
        result["status"] = "success"
    elif out.get("timed_out"):
        result["status"] = f"failed: timed out after {run.timeout_seconds}s"
    elif rc == 0 and not manifest:
        # THE ENGINE RAN AND DID NOT COMPLAIN, AND NOBODY WROTE A MANIFEST. mesh_manifest.json is
        # written by each engine's finalize seam (engines/manifest.write_manifest), which the
        # pipeline executor calls - `run_engine_local` runs the mesher alone and never does. So
        # the absence of a manifest here is a gap in what this harness drives, not a failed mesh,
        # and calling it "failed: mesh_written=False" would publish a loss the run did not take.
        # Scoring the mesh needs finalize wired in; until then this row says so.
        result.update(_unavailable(
            "the mesher returned rc=0 but no mesh_manifest.json was written: this harness drives "
            "`run_engine_local`, which runs the engine without its finalize seam, so there is no "
            "quality evidence for this run to score"))
    else:
        tail = str(out.get("log_tail", ""))[-200:]
        result["status"] = (f"failed: rc={rc}, mesh_written={delivered}"
                            + (f" - {tail}" if tail else ""))
    return result


# ------------------------------------------------------------------------------- rows and summary


def status_class(status: str) -> str:
    head = status.split(":", 1)[0].strip()
    if head not in STATUS_CLASSES:
        raise ValueError(f"unknown row status {status!r}; must start with one of {STATUS_CLASSES}")
    return head


def build_row(scenario: Scenario, result: dict, wall_seconds: float) -> dict:
    """One published row: the spec's manifest fields, filled from the executor's honest result.

    `expected_engine` and `engine_match` are computed HERE, after the run - the executor never saw
    the expectation (see the integrity rule in the module header).
    """
    run = scenario.run
    status = str(result.get("status", ""))
    status_class(status)  # refuse a status outside the vocabulary before it reaches a bundle
    selected = result.get("selected_engine")
    return {
        "scenario": run.name,
        "source": str(run.source_path.relative_to(run.directory)),
        "source_sha256": sha256_file(run.source_path),
        "settings": run.settings,
        "timeout_seconds": run.timeout_seconds,
        "status": status,
        "selected_engine": selected,
        "engine_version": result.get("engine_version"),
        "mesh_statistics": dict(result.get("mesh_statistics") or {}),
        "quality_criteria": list(result.get("quality_criteria") or []),
        "solver_ready": result.get("solver_ready"),
        "timings": {**dict(result.get("timings") or {}),
                    "wall_seconds": round(wall_seconds, 3)},
        "deliverables": list(result.get("deliverables") or []),
        "expected_engine": scenario.expected_engine,
        "engine_match": (None if scenario.expected_engine is None or selected is None
                         else selected == scenario.expected_engine),
    }


def _distribution(values: list[float]) -> dict:
    return {"count": len(values), "min": min(values), "max": max(values),
            "mean": statistics.fmean(values), "median": statistics.median(values)}


def summarise(rows: list[dict]) -> dict:
    """The aggregate, computed only from rows - the spec's mesh-engine metrics: success rate,
    quality distribution (never collapsed to pass/fail), cost, per-engine breakdown, and
    engine-selection accuracy scored over the rows that declared an expectation."""
    statuses: dict[str, int] = {}
    for row in rows:
        key = status_class(row["status"])
        statuses[key] = statuses.get(key, 0) + 1
    successes = [row for row in rows if status_class(row["status"]) == "success"]

    quality: dict[str, list[float]] = {}
    for row in successes:
        for metric, value in (row.get("mesh_statistics") or {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                quality.setdefault(metric, []).append(float(value))

    walls = [float(row.get("timings", {}).get("wall_seconds", 0.0)) for row in rows]
    per_engine: dict[str, dict] = {}
    for row in rows:
        engine = row.get("selected_engine") or "unselected"
        entry = per_engine.setdefault(engine, {"runs": 0, "successes": 0, "walls": []})
        entry["runs"] += 1
        entry["successes"] += int(status_class(row["status"]) == "success")
        entry["walls"].append(float(row.get("timings", {}).get("wall_seconds", 0.0)))
    for entry in per_engine.values():
        walls_e = entry.pop("walls")
        entry["success_rate"] = entry["successes"] / entry["runs"]
        entry["mean_wall_seconds"] = statistics.fmean(walls_e)

    scored = [row for row in rows if row.get("engine_match") is not None]
    matched = sum(1 for row in scored if row["engine_match"])
    ready = [row.get("solver_ready") for row in rows]
    return {
        "scenarios": len(rows),
        "statuses": statuses,
        "mesh_success_rate": (len(successes) / len(rows)) if rows else None,
        "quality_distribution": {metric: _distribution(vals)
                                 for metric, vals in sorted(quality.items())},
        "solver_ready": {"ready": sum(1 for r in ready if r is True),
                         "not_ready": sum(1 for r in ready if r is False),
                         "unknown": sum(1 for r in ready if r is None)},
        "cost": {"total_wall_seconds": sum(walls),
                 "mean_wall_seconds": statistics.fmean(walls) if walls else None,
                 "max_wall_seconds": max(walls) if walls else None},
        "per_engine": per_engine,
        "engine_selection": {"scored": len(scored), "matched": matched,
                             "accuracy": (matched / len(scored)) if scored else None},
    }


# -------------------------------------------------------------------------------------- the run


def run_benchmark(root: Path, out_dir: Path, *, track: str, corpus_name: str,
                  corpus_provenance: str, redistribution_status: str, hardware_class: str,
                  execute: Callable[[ScenarioRun, Path], dict] = execute_scenario,
                  engine_commit: str | None = None, limit: int = 0,
                  scratch: Path | None = None, log: Callable[[str], None] = print) -> Path:
    """Discover, validate, run and seal. Returns the sealed bundle directory."""
    root = Path(root).resolve()
    scenarios = load_scenarios(root)
    if limit:
        scenarios = scenarios[:limit]
    input_files = [path for scenario in scenarios
                   for path in sorted(scenario.run.directory.rglob("*")) if path.is_file()]
    scratch = Path(scratch) if scratch else Path(tempfile.mkdtemp(prefix="mesh_benchmark_"))
    scratch.mkdir(parents=True, exist_ok=True)

    writer = BundleWriter(Path(out_dir))
    rows: list[dict] = []
    for scenario in scenarios:
        started = time.monotonic()
        result = execute(scenario.run, scratch)
        row = build_row(scenario, result, time.monotonic() - started)
        writer.write_row(row)
        rows.append(row)
        log(f"{row['status'][:60]:<62} {row['scenario'][:40]:<42} "
            f"engine={row['selected_engine'] or '-'}")

    settings = {"scenario_root": str(root), "limit": limit,
                "executor": getattr(execute, "__name__", str(execute))}
    manifest = BundleManifest(
        benchmark="mesh-engine", track=track, corpus_name=corpus_name,
        corpus_provenance=corpus_provenance, redistribution_status=redistribution_status,
        input_hashes=hash_inputs(root, input_files),
        engine_commit=(engine_commit
                       or current_engine_commit(Path(__file__).resolve().parents[2])),
        settings_digest="", hardware_class=hardware_class,
        timeout_seconds=max(s.run.timeout_seconds for s in scenarios))
    return writer.seal(manifest, settings=settings, summary=summarise(rows))


def verify_bundle(directory: Path) -> int:
    """Check a sealed mesh bundle against its own rows, the same way the CAD runner's --verify
    does: the stored headline is re-derived from rows.jsonl, because row_count alone cannot catch
    a figure edited in place."""
    try:
        manifest, rows, summary, _ = load_bundle(directory, summarise)
    except (FileNotFoundError, ValueError) as exc:
        print(f"bundle at {directory} is NOT verified: {exc}", file=sys.stderr)
        return 1
    print(f"bundle at {directory} verified: {len(rows)} row(s)\n"
          f"  benchmark      {manifest['benchmark']} / {manifest['track']}\n"
          f"  engine commit  {manifest['engine_commit']}\n"
          f"  success rate   {summary.get('mesh_success_rate')}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the mesh-engine benchmark over frozen scenarios and seal a publication "
                    "bundle (see the module docstring for the scenario format).")
    parser.add_argument("root", nargs="?", default="",
                        help="directory holding the frozen scenario directories")
    # Not argparse-`required`, because --verify is a reading mode that needs none of them; a run
    # still refuses below without them, with the same exit code as before.
    parser.add_argument("--out", default="", help="bundle directory to write and seal")
    parser.add_argument("--track", default="",
                        help="e.g. clean-geometry, cad-repair-downstream, engine-selection")
    parser.add_argument("--corpus-name", default="")
    parser.add_argument("--provenance", default="",
                        help="where the inputs came from, with fetch instructions")
    parser.add_argument("--redistribution-status", default="",
                        help="redistributable | non-redistributable | review-pending | internal")
    parser.add_argument("--hardware-class", default="", help="e.g. dev-host-arm64")
    parser.add_argument("--limit", type=int, default=0, help="stop after N scenarios")
    parser.add_argument("--verify", default="",
                        help="check a sealed bundle instead of running: its summary is re-derived "
                             "from rows.jsonl and nothing is meshed")
    args = parser.parse_args()

    if args.verify:
        return verify_bundle(Path(args.verify))

    missing = [flag for flag, value in (
        ("root", args.root), ("--out", args.out), ("--track", args.track),
        ("--corpus-name", args.corpus_name), ("--provenance", args.provenance),
        ("--redistribution-status", args.redistribution_status),
        ("--hardware-class", args.hardware_class)) if not value]
    if missing:
        parser.error(f"a benchmark run needs {', '.join(missing)} (or use --verify <bundle>)")
    try:
        bundle = run_benchmark(
            Path(args.root), Path(args.out), track=args.track, corpus_name=args.corpus_name,
            corpus_provenance=args.provenance, redistribution_status=args.redistribution_status,
            hardware_class=args.hardware_class, limit=args.limit)
    except (ScenarioError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"\nsealed bundle: {bundle}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
