#!/usr/bin/env python3
# Responsibility: Decide whether a tier's structured record proves the tier actually ran.
# Boundaries: it reads one junit file and exits; it runs no test and knows nothing about tiers.
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET

# WHY THIS IS A FILE. It used to be a heredoc inside run.sh, where the one thing it exists to
# prevent - a run that proves nothing being read as a pass - could not itself be tested. A summary
# line is not evidence: "0 passed" and "no tests ran" both read as green to a human skimming.


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: assert_record.py <junit.xml> <tier> [allowed-skip-substring]", file=sys.stderr)
        return 2
    xml, tier = argv[1], argv[2]
    allow = argv[3] if len(argv) > 3 else ""

    # noqa S314: the input is the junit file pytest just wrote under this runner's own RESULTS_DIR,
    # one step earlier in the same script. It is this repository's output, not untrusted input, and
    # reaching for defusedxml would add a dependency to read a file we produced.
    root = ET.parse(xml).getroot()  # noqa: S314
    suites = root.findall("testsuite") or [root]
    total = sum(int(s.get("tests", 0)) for s in suites)
    fail = sum(int(s.get("failures", 0)) + int(s.get("errors", 0)) for s in suites)
    skips = []
    for s in suites:
        for case in s.iter("testcase"):
            for sk in case.findall("skipped"):
                skips.append((case.get("classname", ""), case.get("name", ""),
                              sk.get("message", "")))

    print(f"  {tier}: {total} collected, {fail} failed/errored, {len(skips)} skipped")
    if total == 0:
        print(f"  FATAL: {tier} collected ZERO tests - an empty run is not a passing run",
              file=sys.stderr)
        return 1
    bad = [s for s in skips if allow not in s[2]] if allow else skips
    if bad:
        for c, n, m in bad[:20]:
            print(f"    UNEXPECTED SKIP {c}::{n} - {m}")
        print(f"  FATAL: {tier} skipped {len(bad)} test(s) outside its policy - a skipped contract "
              "is not a proven one", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
