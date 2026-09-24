#!/usr/bin/env python3
# Responsibility: Prove the unit lane collects cleanly, so a lane that ran nothing can never read as a lane that passed.
# Owns: the floor on how much of the suite must be collectable, and the rule that every area of it is present.
# Boundaries: it collects; it runs no test and asserts nothing about what the tests say.

# WHAT HAPPENED. Three test modules called tests/_surveyor_package.require at MODULE SCOPE. The
# geometry-agent distribution was not installed in any CI lane, require() fails loudly rather than
# skipping, and a failure raised during import is a COLLECTION ERROR. pytest answers three of those
# with
#
#     !!!!!!! Interrupted: 3 errors during collection !!!!!!!
#     7252/7269 tests collected (17 deselected), 3 errors
#
# and stops. Seven thousand two hundred and fifty two tests were collected and NONE of them ran, on
# every commit, for as long as that was true. The lane was red, so nothing was technically hidden -
# and "3 errors during collection" reads like three broken tests rather than a suite that said nothing
# about anything. The loudness was aimed at the right thing and landed at the wrong granularity.
#
# BOTH HALVES ARE FIXED ELSEWHERE: tests/_surveyor_package.py now turns a module-scope gate into a
# failure of that module's own tests, and .github/actions/setup-hexera installs the vendored wheel so
# the absence does not arise in CI at all. This is the third thing, the one that does not depend on
# either of those being right: a check that the suite is THERE, run before the suite runs.
#
# WHAT IT REFUSES
#
#   any collection error      a file that cannot be imported, whatever the reason
#   fewer than FLOOR tests    the suite is not a ratchet, but a run that collects a tenth of it has
#                             lost a whole tree and nobody meant that
#   an EMPTY area             every directory under tests/unit must yield at least one collected test,
#                             which is what catches one area going dark while the total stays plausible
#
#   python devtools/quality/check_unit_collection.py
#   python devtools/quality/check_unit_collection.py --quiet
#
# Exit 0 when the suite collects, 1 when it does not, 2 when this check could not run pytest at all.
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UNIT = ROOT / "tests" / "unit"

#: The selection `make test-fast` uses, so this collects what CI is about to run and not something
#: adjacent to it.
DESELECT = "not external_fixture"

#: A TRIPWIRE, NOT A RATCHET. The suite collected 7269 tests when this was written and the number is
#: meant to move with the suite, which is why the floor sits well below it: what this catches is a run
#: that lost a tree, not a commit that deleted a test. Raise it when the suite grows by a lot; never
#: lower it to make a run pass.
FLOOR = 6000

_TEST_ID = re.compile(r"^(tests/unit/[^\s:]+)::")


def collect() -> tuple[int, list[str], str]:
    """Collect the unit tier and return (exit code, collected test ids, combined output)."""
    # PYTEST_ADDOPTS IS INHERITED ON PURPOSE. CI sets it to --randomly-seed=<n>, and this has to collect
    # what the lane is about to run rather than something adjacent to it. That also rules out
    # `-p no:randomly` here: disabling pytest-randomly removes the very option the inherited addopts
    # passes, and the collection would then exit on an unrecognised argument instead of collecting.
    # Collection order is irrelevant to a count, so there is nothing to gain by fixing the order anyway.
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/unit", "-m", DESELECT,
         "--collect-only", "-q", "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=str(ROOT), timeout=3600, check=False)
    output = done.stdout + done.stderr
    # Backslashes normalised because pytest prints node ids with the platform's separator. Without this
    # the whole parse yields nothing on Windows and the gate refuses a tree that is perfectly fine,
    # which is the opposite failure to the one it exists for but just as useless.
    ids = [line.strip().replace("\\", "/") for line in done.stdout.splitlines()
           if "::" in line and line.strip().lower().startswith("tests")]
    return done.returncode, ids, output


def areas_on_disk() -> set[str]:
    """Every directory under tests/unit that holds test files."""
    found = set()
    for path in UNIT.rglob("test_*.py"):
        found.add(path.relative_to(UNIT).parts[0] if path.parent != UNIT else ".")
    return found


def check(*, quiet: bool = False) -> int:
    try:
        code, ids, output = collect()
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"FAILED: pytest could not be run at all: {type(exc).__name__}: {exc}")
        return 2

    problems: list[str] = []
    if code != 0:
        tail = "\n".join(output.strip().splitlines()[-12:])
        problems.append(
            f"collection exited {code}. A collection error means the files it names were never even "
            f"imported, so every test in them - and, when pytest interrupts, every test anywhere - "
            f"ran zero times:\n{tail}")
    if len(ids) < FLOOR:
        problems.append(
            f"only {len(ids)} tests collected, and the floor is {FLOOR}. The suite is not a ratchet, "
            f"so this is not about a deleted test: a number this low means a tree stopped being "
            f"collected. If the suite genuinely shrank this much, move FLOOR deliberately.")

    seen_areas = set()
    for test_id in ids:
        match = _TEST_ID.match(test_id)
        if match:
            parts = Path(match.group(1)).relative_to("tests/unit").parts
            seen_areas.add(parts[0] if len(parts) > 1 else ".")
    dark = sorted(areas_on_disk() - seen_areas)
    if dark:
        problems.append(
            f"these areas hold test files and contributed no collected test: {dark}. One area going "
            f"quiet while the total stays plausible is the version of this defect a count alone "
            f"cannot see.")

    if not quiet:
        print(f"collected {len(ids)} tests from {len(seen_areas)} area(s) under tests/unit "
              f"(selection: -m {DESELECT!r})")
    if problems:
        print("\nFAILED: the unit tier does not collect cleanly")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"OK: {len(ids)} tests collected, no collection errors, every area present")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quiet", action="store_true", help="print the verdict only")
    args = ap.parse_args(argv)
    return check(quiet=args.quiet)


if __name__ == "__main__":
    raise SystemExit(main())
