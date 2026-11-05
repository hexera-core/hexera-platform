#!/usr/bin/env python3
# Responsibility: Split a tracked test-file set into N shards that are total, disjoint and stable.
# Owns: the file inventory, the balancing rule, and the refusal to emit a shard nobody would notice was empty.
# Boundaries: it prints paths; it runs no tests and reads no test contents.

# WHY THIS EXISTS. The unit tier is 503 files in one 9m46s pytest session, and the integration tier
# is one 10m26s session behind a container build. Both are the critical path of a run whose next
# slowest lane finishes in under three minutes. Splitting them across runners is the only lever
# left - nothing in either suite is waiting on anything, they are simply queued behind each other.
#
# THE ONE THING SHARDING MUST NOT DO IS LOSE A TEST, and the usual way it happens is a hand-written
# matrix of directories that nobody updates when a new directory appears: the tests are collected
# by nothing, the lane is green, and the gap is invisible because a test that never ran reports
# nothing at all. So the inventory here is `git ls-files` - the tracked tree, not a list somebody
# maintains - and the partition is index-based, so every file is in exactly one shard by
# construction. tests/unit/hygiene/test_test_sharding_is_total.py asserts both properties against
# the shard count the workflow actually uses.
#
# BALANCE IS BY MEASURED TIME, descending, into the currently-lightest shard (greedy LPT). It used
# to be by file size, which is deterministic but put most of the slow files into one unit shard
# (18m25s against ~7m for the other five). The timings live in test_durations.json, refreshed with
# `--record <junit.xml>...`; a stale or missing entry only costs balance, never totality, because
# an unmeasured file is weighed by its size and every file still lands in exactly one shard.
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def tracked_test_files(root: str) -> list[str]:
    """Every tracked `test_*.py` under `root`, sorted.

    `git ls-files` rather than a glob walk: it is the tracked tree, so an untracked scratch file
    cannot join a shard and a file somebody forgot to `git add` cannot silently be scheduled.
    """
    out = subprocess.run(
        ["git", "ls-files", f"{root}/**/test_*.py", f"{root}/test_*.py"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()
    return sorted(set(out))


#: Measured seconds per test file, written by `--record` from CI's JUnit reports. Weighing by file
#: SIZE put most of the slow files in one unit shard (18 min against 7 for its siblings); measured
#: time is what a shard actually costs. A file with no measurement yet - a new one - is weighed by
#: its size at the suite's average seconds per byte, so it still lands somewhere sensible.
DURATIONS = ROOT / "devtools/quality/test_durations.json"


def _durations() -> dict[str, float]:
    if not DURATIONS.is_file():
        return {}
    return {k: float(v) for k, v in json.loads(DURATIONS.read_text(encoding="utf-8")).items()}


def _weights(files: list[str]) -> dict[str, float]:
    measured = _durations()
    sizes = {p: (ROOT / p).stat().st_size for p in files}
    known = [p for p in files if p in measured]
    known_bytes = sum(sizes[p] for p in known)
    rate = (sum(measured[p] for p in known) / known_bytes) if known_bytes else 1.0
    return {p: measured[p] if p in measured else sizes[p] * rate for p in files}


def partition(files: list[str], shards: int) -> list[list[str]]:
    """Greedy longest-processing-time bins, by measured duration (file size when unmeasured).

    Total and disjoint by construction: every file is appended to exactly one bin. The sort is
    (-weight, path) so equal weights order by name and the partition is stable across machines.
    """
    if shards < 1:
        raise ValueError("shards must be >= 1")
    weight = _weights(files)
    weighed = sorted(files, key=lambda p: (-weight[p], p))
    bins: list[list[str]] = [[] for _ in range(shards)]
    load = [0.0] * shards
    for path in weighed:
        target = load.index(min(load))
        bins[target].append(path)
        load[target] += weight[path]
    return [sorted(b) for b in bins]


def record(reports: list[str]) -> int:
    """Merge the per-file seconds in JUnit `reports` into DURATIONS (one rounded number a file)."""
    seconds: dict[str, float] = {}
    for report in reports:
        for case in ET.parse(report).getroot().iter("testcase"):  # noqa: S314 - our own CI reports
            path = case.get("file") or ""
            if not path:
                parts = (case.get("classname") or "").split(".")
                mods = [i for i, part in enumerate(parts) if part.startswith("test_")]
                if not mods:
                    continue
                path = "/".join(parts[: mods[0] + 1]) + ".py"
            seconds[path] = seconds.get(path, 0.0) + float(case.get("time") or 0)
    if not seconds:
        print("::error:: no test cases in the given reports", file=sys.stderr)
        return 1
    merged = {**_durations(), **{k: round(v, 1) for k, v in seconds.items()}}
    tracked = set(tracked_test_files("tests"))
    merged = {k: v for k, v in sorted(merged.items()) if k in tracked}
    DURATIONS.write_text(json.dumps(merged, indent=0, sort_keys=True) + "\n", encoding="utf-8")
    print(f"recorded {len(seconds)} files into {DURATIONS.relative_to(ROOT)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("root", nargs="?", help="tree to shard, e.g. tests/unit")
    ap.add_argument("--shard", type=int, help="1-based shard index")
    ap.add_argument("--of", type=int, help="total shard count")
    ap.add_argument("--null", action="store_true", help="NUL-separate, for xargs -0")
    ap.add_argument("--record", nargs="+", metavar="JUNIT",
                    help="merge per-file seconds from these JUnit reports into test_durations.json")
    args = ap.parse_args(argv)

    if args.record:
        return record(args.record)
    if not args.root or args.shard is None or args.of is None:
        ap.error("root, --shard and --of are required unless --record is given")

    if args.of < 1 or not 1 <= args.shard <= args.of:
        print(f"::error:: shard {args.shard} of {args.of} is not a shard", file=sys.stderr)
        return 2

    files = tracked_test_files(args.root)
    if not files:
        # The scan subject being empty is the one failure that looks exactly like success: pytest
        # given no files exits 0 having run nothing.
        print(f"::error:: no tracked test files under {args.root} - the shard subject is empty",
              file=sys.stderr)
        return 1
    if args.of > len(files):
        print(f"::error:: {args.of} shards for {len(files)} files leaves shards with nothing to "
              f"run, and an empty pytest invocation exits 0", file=sys.stderr)
        return 1

    chosen = partition(files, args.of)[args.shard - 1]
    sys.stdout.write(("\0" if args.null else "\n").join(chosen))
    sys.stdout.write("\0" if args.null else "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
