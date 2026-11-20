#!/usr/bin/env python3
"""Measure isolated CRS repair episodes without trusting in-process native state."""

from __future__ import annotations

import argparse
import json
import os
import resource
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
CORPUS_RUNNER = REPO / "devtools" / "validation" / "repair_corpus.py"
DETERMINISM_FIELDS = (
    "valid_before",
    "defects_before",
    "located_before",
    "status",
    "route",
    "outcome",
    "operations",
    "valid_after",
    "defects_after",
    "located_after",
    "repaired_defects",
)


def _summary(values: list[float]) -> dict[str, float]:
    return {
        "min": min(values),
        "median": statistics.median(values),
        "max": max(values),
    }


def _determinism(episodes: list[dict[str, Any]]) -> dict[str, Any]:
    baselines: dict[str, dict[str, Any]] = {}
    repeats: dict[str, int] = {}
    mismatches = []
    for episode in episodes:
        row = episode["row"]
        file_name = str(row["file"])
        repeats[file_name] = repeats.get(file_name, 0) + 1
        result = {field: row.get(field) for field in DETERMINISM_FIELDS}
        if file_name not in baselines:
            baselines[file_name] = result
            continue
        fields = sorted(field for field in DETERMINISM_FIELDS if result[field] != baselines[file_name][field])
        if fields:
            mismatches.append({"file": file_name, "repeat": repeats[file_name], "fields": fields})
    return {"deterministic": not mismatches, "mismatches": mismatches}


def _run_episode(
    path: Path, planner: str, *, timeout_seconds: float = 600.0, artifact_dir: Path | None = None
) -> dict[str, Any]:
    """Launch a fresh supervisor; its native child can abort without killing this process."""
    if artifact_dir is None:
        artifact_dir = Path(tempfile.mkdtemp(prefix="crs-repair-phase0-"))
    else:
        artifact_dir.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        str(path),
        "--planner",
        planner,
        "--artifact-dir",
        str(artifact_dir),
    ]
    started = time.perf_counter()
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        return {
            "row": {"file": str(path), "outcome": "timeout", "located_after": None, "repaired_defects": None},
            "wall_seconds": time.perf_counter() - started,
            "cpu_seconds": 0.0,
            "max_rss_kb": 0,
            "raw_rows_path": str(artifact_dir / "rows.json"),
            "error": f"episode timed out after {exc.timeout} seconds",
        }
    if process.returncode:
        return {
            "row": {"file": str(path), "outcome": "worker_crash", "located_after": None, "repaired_defects": None},
            "wall_seconds": time.perf_counter() - started,
            "cpu_seconds": 0.0,
            "max_rss_kb": 0,
            "raw_rows_path": str(artifact_dir / "rows.json"),
            "error": stderr.strip() or f"worker exited {process.returncode}",
        }
    result = json.loads(stdout)
    result["wall_seconds"] = time.perf_counter() - started
    return result


def measure_inputs(
    paths: list[Path], repeats: int = 3, planner: str = "off", *, artifact_root: Path | None = None
) -> dict[str, Any]:
    if repeats < 1:
        raise ValueError("repeats must be at least 1")
    if planner not in {"off", "shadow", "live"}:
        raise ValueError(f"unsupported planner mode: {planner}")
    episodes = []
    for path_index, path in enumerate(paths):
        for repeat in range(repeats):
            artifact_dir = (
                artifact_root / f"{path_index:04d}-{Path(path).stem}-r{repeat + 1}" if artifact_root else None
            )
            episodes.append(_run_episode(Path(path), planner, artifact_dir=artifact_dir))
    return {
        "rows": episodes,
        "wall_seconds": _summary([row["wall_seconds"] for row in episodes]),
        "cpu_seconds": _summary([row["cpu_seconds"] for row in episodes]),
        "max_rss_kb": max(row["max_rss_kb"] for row in episodes),
        "determinism": _determinism(episodes),
    }


def _worker(path: Path, planner: str, artifact_dir: Path) -> int:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    rows_path = artifact_dir / "rows.json"
    # The artifact directory can be committed evidence; corpus CAD files must never land in it.
    with tempfile.TemporaryDirectory(prefix="crs-repair-phase0-input-") as staging:
        input_dir = Path(staging)
        shutil.copy2(path, input_dir / path.name)
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        process = subprocess.run(
            [
                sys.executable,
                str(CORPUS_RUNNER),
                str(input_dir),
                "--limit",
                "1",
                "--planner",
                planner,
                "--json",
                str(rows_path),
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu_seconds = (after.ru_utime + after.ru_stime) - (before.ru_utime + before.ru_stime)
    max_rss = after.ru_maxrss
    if sys.platform == "darwin":
        max_rss /= 1024
    if process.returncode or not rows_path.exists():
        row = {"file": str(path), "outcome": "native_process_error", "located_after": None, "repaired_defects": None}
        error = process.stderr.strip() or process.stdout.strip() or f"corpus exited {process.returncode}"
    else:
        rows = json.loads(rows_path.read_text())
        row = rows[0]
        row["file"] = str(path)
        error = ""
    result = {
        "row": row,
        "cpu_seconds": cpu_seconds,
        "max_rss_kb": int(max_rss),
        "raw_rows_path": str(rows_path),
    }
    if error:
        result["error"] = error
    sys.stdout.write(json.dumps(result))
    return 0


def _input_files(inputs: list[str], limit: int) -> list[Path]:
    files = []
    for value in inputs:
        path = Path(value)
        if path.is_file() and path.suffix.lower() in {".step", ".stp"}:
            files.append(path)
        elif path.is_dir():
            files.extend(p for p in sorted(path.rglob("*")) if p.is_file() and p.suffix.lower() in {".step", ".stp"})
    return files[:limit] if limit else files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="*")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--planner", choices=("off", "shadow", "live"), default="off")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--artifact-dir", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        return _worker(args.worker, args.planner, args.artifact_dir)
    files = _input_files(args.inputs, args.limit)
    if not files:
        parser.error("no .step/.stp inputs found")
    artifact_root = args.out.parent / f"{args.out.stem}-raw" if args.out else None
    result = measure_inputs(files, repeats=args.repeats, planner=args.planner, artifact_root=artifact_root)
    output = json.dumps(result, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(output + "\n")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
