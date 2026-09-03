#!/usr/bin/env python3
# Responsibility: Prove the integration tier actually exercised real PostgreSQL, rather than merely exiting zero.
# Boundaries: it reads the structured JUnit result, not console text.
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET

#: The named guarantees that exist ONLY against real PostgreSQL. If the harness stops provisioning
#: a database these are the first things to vanish, and their absence is what this guard is for.
#: Matched as (module-suffix, test-name) so a parametrised node matches every one of its cases.
REQUIRED = {
    "tenant isolation": (
        "test_geometry_source_providers", "test_another_tenant_cannot_resolve_the_source"),
    "tenant isolation (snapshot)": (
        "test_geometry_source_providers", "test_a_snapshot_naming_another_tenant_is_not_authorized"),
    "checkpoint ownership fencing": (
        "test_checkpoint_restart_postgres", "test_the_abandoned_worker_is_fenced_at_each_point"),
}

#: The ONLY skips a correctly provisioned run may contain. Both are genuinely optional inputs a
#: clean clone does not have, not services this runner is responsible for starting:
#:   - the licensed CAD STEP fixture, which is not redistributable
#:   - the two restricted MinIO identities, which exercise a provider's own IAM refusals
ALLOWED_SKIP_SUBSTRINGS = (
    "real STEP not staged",
    "restricted MinIO identity not provisioned",
    # A compose stack brought up INSIDE the containerised tier would be a container nested in a
    # container, which is not the isolation these tests describe. This string is emitted only when
    # the runner marks the tier; a host run with no daemon still says "docker is unavailable here"
    # and still fails here, which is the case worth catching.
    "compose stacks are a host-tier concern",
)

#: A floor, not a target. The database-backed modules contribute far more than this; the point is
#: that "collected nothing" and "collected only the hermetic subset" both fail loudly.
MIN_EXECUTED = 300


def _classname_module(case: ET.Element) -> str:
    return (case.get("classname") or "").split(".")[-1]


def main(path: str) -> int:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        print(f"::error:: no readable JUnit report at {path}: {exc}")
        print("         pytest produced no structured result - the run cannot be called green.")
        return 1

    cases = list(root.iter("testcase"))
    problems: list[str] = []

    if not cases:
        problems.append("the report contains ZERO test cases - a collection-only, empty or fully "
                        "deselected run is not a pass")

    skipped, failed, executed = [], [], []
    for c in cases:
        if c.find("skipped") is not None:
            skipped.append(c)
        else:
            executed.append(c)
            if c.find("failure") is not None or c.find("error") is not None:
                failed.append(c)

    if len(executed) < MIN_EXECUTED:
        problems.append(
            f"only {len(executed)} tests actually executed (floor {MIN_EXECUTED}). The "
            "database-backed tier is missing - this is the silent-skip failure itself")

    for c in failed:
        problems.append(f"failed: {_classname_module(c)}::{c.get('name')}")

    # Every skip must be one of the two optional fixtures. A skip for a MISSING SERVICE is the
    # exact regression this guard exists to catch, so it is reported with its own reason text.
    for c in skipped:
        node = c.find("skipped")
        reason = (node.get("message") or node.text or "") if node is not None else ""
        if not any(a in reason for a in ALLOWED_SKIP_SUBSTRINGS):
            problems.append(
                f"skipped for a non-optional reason: {_classname_module(c)}::{c.get('name')} "
                f"-- {reason.strip()[:160]}")

    # The named guarantees must be present AND passing - not merely 'not failed', which a skipped
    # or absent test also satisfies.
    for label, (module, name) in REQUIRED.items():
        matches = [c for c in cases
                   if _classname_module(c) == module and (c.get("name") or "").startswith(name)]
        if not matches:
            problems.append(f"{label}: {module}::{name} did not run at all")
            continue
        ran = [c for c in matches if c.find("skipped") is None]
        if not ran:
            problems.append(f"{label}: {module}::{name} was SKIPPED, so the guarantee is unproven")
            continue
        bad = [c for c in ran if c.find("failure") is not None or c.find("error") is not None]
        if bad:
            problems.append(f"{label}: {module}::{name} did not pass")

    if problems:
        print("::error:: integration coverage is vacuous or incomplete:")
        for p in problems:
            print(f"    - {p}")
        return 1

    print(f"coverage guard OK: {len(executed)} executed, {len(skipped)} optional skips, "
          f"{len(REQUIRED)} named real-PostgreSQL guarantees proven")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: assert_integration_coverage.py <junit-report.xml>")
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
