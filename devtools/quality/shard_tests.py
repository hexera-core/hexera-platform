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
# BALANCE IS BY FILE SIZE, descending, into the currently-lightest shard (greedy LPT). Byte count
# is a proxy for runtime and an imperfect one, but it is DETERMINISTIC and needs no recorded
# timings file - and a timings file is a second thing to keep in step with the tree, which is the
# failure this module exists to avoid. Round-robin over a sorted list was the alternative and it
# balances badly: tests/unit/engines alone is 96 files, and its largest are far from average.
from __future__ import annotations

import argparse
import subprocess
import sys
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


def partition(files: list[str], shards: int) -> list[list[str]]:
    """Greedy longest-processing-time bins, by file size.

    Total and disjoint by construction: every file is appended to exactly one bin. The sort is
    (-size, path) so equal-sized files order by name and the partition is stable across machines.
    """
    if shards < 1:
        raise ValueError("shards must be >= 1")
    weighed = sorted(files, key=lambda p: (-(ROOT / p).stat().st_size, p))
    bins: list[list[str]] = [[] for _ in range(shards)]
    load = [0] * shards
    for path in weighed:
        target = load.index(min(load))
        bins[target].append(path)
        load[target] += (ROOT / path).stat().st_size
    return [sorted(b) for b in bins]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("root", help="tree to shard, e.g. tests/unit")
    ap.add_argument("--shard", type=int, required=True, help="1-based shard index")
    ap.add_argument("--of", type=int, required=True, help="total shard count")
    ap.add_argument("--null", action="store_true", help="NUL-separate, for xargs -0")
    args = ap.parse_args(argv)

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
