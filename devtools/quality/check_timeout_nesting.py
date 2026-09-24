#!/usr/bin/env python3
# Responsibility: Prove every inner deadline on the geometry paths is shorter than the outer one that kills it.
# Owns: the list of nested deadline pairs, and which of them this repository can fix.
# Boundaries: a repository gate; it reads the application's own numbers and the measurement package's, and runs no job.

# THE LAW: AN INNER DEADLINE MUST ALWAYS BE SHORTER THAN THE OUTER ONE THAT KILLS IT.
#
# THE DEFECT. `measure_source` was declared with `soft_time_limit=1200, time_limit=1500` as though
# 1500 s bounded the work inside it. It did not. The measurement runs in a child process and the
# package retries that child twice on a native fault or an out-of-memory kill, applying the configured
# deadline to EACH attempt: 3 x 900 + 2 x 3 = 2706 s. So a part that faulted twice was killed by Celery
# part way through its second retry, before the line that records the outcome ever ran. Both numbers
# were plausible on their own. The relation between them was never stated, so nothing could check it.
#
# WHY A GATE AND NOT A COMMENT. A pair of numbers that must satisfy an inequality does not keep
# satisfying it when two people maintain them a year apart. src/meshpipeline/adapters/
# pipeline_execution/budgets.py now DERIVES each outer kill from the inner budget, which is the real
# fix; this gate is what notices when a new pair appears that was not derived, or when a number moves
# in the package this repository does not own.
#
# WHAT IT COMPARES. Each row is a nested pair: something that runs, and something that stops it.
# Rows this repository owns must nest, and a row that does not is a FAILURE. One row is owned
# elsewhere - the inline upload path in application/geometry_measurement.py - and it is REPORTED on
# every run with its numbers, so that it can neither be forgotten nor quietly become a pass.
#
#   python devtools/quality/check_timeout_nesting.py
#   python devtools/quality/check_timeout_nesting.py --quiet
#
# Exit 0 when every owned pair nests, 1 when one does not, 2 when the check could not read the numbers.
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Pair:
    """One nested deadline: `inner` runs, `outer` ends it."""

    what: str
    inner_s: float
    inner_is: str
    outer_s: float
    outer_is: str
    owned_here: bool = True
    note: str = ""

    @property
    def nests(self) -> bool:
        return self.inner_s < self.outer_s


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _literal(path: str, name: str) -> float:
    """One module-level float or int literal, read out of the source rather than imported.

    The file this reads from is not on this gate's import path in every environment, and importing it
    would pull the whole application in to learn one number.
    """
    text = _source(path)
    found = re.search(rf"^{re.escape(name)}\s*[:=][^=\n]*?=?\s*([0-9]+(?:\.[0-9]+)?)\s*$",
                      text, re.MULTILINE)
    if not found:
        found = re.search(rf"^{re.escape(name)}\s*=\s*([0-9]+(?:\.[0-9]+)?)", text, re.MULTILINE)
    if not found:
        raise LookupError(f"{name} is not a plain numeric literal in {path}; this gate reads it as one")
    return float(found.group(1))


def pairs() -> list[Pair]:
    """Every nested deadline on the geometry paths, with both numbers read from where they live."""
    sys.path.insert(0, str(ROOT / "src"))
    from meshpipeline.adapters.pipeline_execution import budgets

    rows = [
        Pair(what="the measurement worker: the package's whole retry budget, inside Celery's soft kill",
             inner_s=budgets.MEASURE_PACKAGED_BUDGET_S,
             inner_is=f"{budgets.ISOLATED_ATTEMPTS} attempts x {budgets.MEASURE_PER_ATTEMPT_S:.0f}s "
                      f"+ {budgets.ISOLATED_ATTEMPTS - 1} waits x {budgets.ISOLATED_RETRY_WAIT_S:.0f}s",
             outer_s=budgets.MEASURE_SOFT_TIME_LIMIT_S,
             outer_is="soft_time_limit on tasks.geometry.measure_source"),
        Pair(what="the measurement worker: Celery's soft kill, inside its hard kill",
             inner_s=budgets.MEASURE_SOFT_TIME_LIMIT_S,
             inner_is="soft_time_limit, which raises inside the task so the row can record what happened",
             outer_s=budgets.MEASURE_TIME_LIMIT_S,
             outer_is="time_limit, which kills the process and records nothing"),
        Pair(what="the look worker: the look's whole-call deadline, inside Celery's soft kill",
             inner_s=budgets.LOOK_INNER_BUDGET_S,
             inner_is="GEOMETRY_VISION_TIMEOUT_SECONDS, joined as a thread deadline by the package",
             outer_s=budgets.LOOK_SOFT_TIME_LIMIT_S,
             outer_is="soft_time_limit on tasks.geometry.look_at_source"),
        Pair(what="the look worker: Celery's soft kill, inside its hard kill",
             inner_s=budgets.LOOK_SOFT_TIME_LIMIT_S,
             inner_is="soft_time_limit",
             outer_s=budgets.LOOK_TIME_LIMIT_S,
             outer_is="time_limit"),
    ]

    # THE INLINE UPLOAD PATH, which this repository's owner of that file has to fix. A small upload is
    # measured inside the HTTP request: the ceiling is passed to the package as the per-attempt
    # deadline, and the request waits ceiling + 5 s. The package's retry budget for that per-attempt
    # deadline is three times as long, so the request stops waiting while the thread and its child
    # carry on. It ABANDONS rather than kills - nothing is lost, and a thread and a child process
    # outlive the response that said the measurement was skipped - which is why it is reported rather
    # than failed here. The fix is one line and it is named in the note.
    inline_source = "src/meshpipeline/application/geometry_measurement.py"
    ceiling = _literal(inline_source, "SYNCHRONOUS_DEADLINE_CEILING_S")
    inline_fits = budgets.largest_per_attempt_within(ceiling)
    # WHAT THE INLINE PATH HANDS THE PACKAGE, and this is the line that lets this gate ever say
    # "fixed". Today it hands the ceiling itself as the PER-ATTEMPT deadline, so the packaged budget is
    # three times the wait in front of it. A gate that only ever read the ceiling could never report
    # that fixed however the fix was written, and a check that cannot say "fixed" gets ignored. So it
    # looks for the one call that derives the per-attempt deadline from this law.
    derived = "largest_per_attempt_within" in _source(inline_source)
    per_attempt = inline_fits if derived else ceiling
    rows.append(Pair(
        what="the inline upload path: the package's retry budget, inside the request's own wait",
        inner_s=budgets.packaged_measurement_budget_s(per_attempt),
        inner_is=f"{budgets.ISOLATED_ATTEMPTS} attempts x {per_attempt:.0f}s "
                 f"+ {budgets.ISOLATED_ATTEMPTS - 1} waits x {budgets.ISOLATED_RETRY_WAIT_S:.0f}s"
                 + ("" if derived else " (the ceiling, handed over as a per-attempt deadline)"),
        outer_s=ceiling + 5.0,
        outer_is="asyncio.wait_for(deadline + 5.0) in geometry_measurement.on_upload",
        owned_here=False,
        note=f"pass budgets.largest_per_attempt_within(SYNCHRONOUS_DEADLINE_CEILING_S) "
             f"({inline_fits:.0f}s) as the per-attempt deadline instead of the ceiling itself "
             f"({ceiling:.0f}s). The slowest corpus file measures in 12.32 s, so {inline_fits:.0f}s "
             f"is still comfortably above it."))
    return rows


def check(*, quiet: bool = False) -> int:
    try:
        rows = pairs()
    except Exception as exc:  # noqa: BLE001 - an unrunnable check is not a passing one
        print(f"FAILED: the deadlines could not be read: {type(exc).__name__}: {exc}")
        print("  A gate that cannot read the numbers says nothing about them, so it refuses.")
        return 2
    if not rows:
        print("FAILED: no nested deadline pairs were found at all, which cannot be true. An empty "
              "comparison is how a check acquires the blind spot it is checking for.")
        return 2

    broken = [r for r in rows if r.owned_here and not r.nests]
    if not quiet:
        for row in rows:
            mark = "ok  " if row.nests else "BAD "
            if not row.owned_here:
                mark = "ok  " if row.nests else "KNOWN VIOLATION, NOT OWNED HERE"
            print(f"{mark} {row.what}")
            print(f"       inner {row.inner_s:8.0f}s  {row.inner_is}")
            print(f"       outer {row.outer_s:8.0f}s  {row.outer_is}")
            if row.note and not row.nests:
                print(f"       fix   {row.note}")
    if broken:
        print("\nFAILED: an inner deadline outlives the outer one that kills it")
        for row in broken:
            print(f"  - {row.what}: inner {row.inner_s:.0f}s >= outer {row.outer_s:.0f}s. A job can be "
                  f"killed part way through the work inside it, which leaves whatever a half-attempt "
                  f"leaves and writes no row saying so.")
        return 1
    unowned = [r for r in rows if not r.owned_here and not r.nests]
    print(f"\nOK: {len(rows) - len(unowned)} nested deadline pair(s) nest correctly"
          + (f"; {len(unowned)} known violation(s) in files this check does not own, reported above"
             if unowned else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quiet", action="store_true", help="print the verdict only")
    args = ap.parse_args(argv)
    return check(quiet=args.quiet)


if __name__ == "__main__":
    raise SystemExit(main())
